import json
from datetime import date
from functools import wraps

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.core.validators import EmailValidator, URLValidator
from django.db import IntegrityError, transaction
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods, require_POST

from .api_tokens import (
    ApiTokenError,
    authenticate_access_token,
    issue_token_pair,
    rotate_refresh_token,
)
from .api_documentation import API_DOCUMENTATION, API_DOCUMENTATION_ORDER
from .forms import OutreachForm
from .models import (
    ApiAccessToken,
    CompanyIdentity,
    CompanyUpdateAudit,
    Outreach,
    OutreachUpdateAudit,
    Profile,
    Prospect,
    ProspectUpdateAudit,
)
from .permissions import is_manager
from .workflows import sync_prospect_from_outreach


API_COMPANY_FIELDS = {
    "short_description": ("short_description", 1000),
    "consulting_focus": ("consulting_focus", 300),
    "client_segment": ("client_segment", 300),
    "eligibility_evidence": ("eligibility_evidence", 5000),
    "contact_name": ("contact_name", 200),
    "contact_title": ("contact_title", 200),
    "linkedin_url": ("contact_linkedin_url", 500),
    "email": ("contact_email", 254),
    "phone": ("contact_phone", 50),
}

API_PROSPECT_STRING_FIELDS = {
    "founder_account": ("founder_account", 30, Prospect.FounderAccount.values),
    "linkedin_connection_status": (
        "linkedin_connection_status",
        30,
        Prospect.LinkedInConnectionStatus.values,
    ),
    "personalization_note": ("personalization_note", 500, None),
    "founder_escalation_notes": ("founder_escalation_notes", 5000, None),
    "material_shared": ("material_shared", 30, Prospect.MaterialShared.values),
    "interest_signal": ("interest_signal", 5000, None),
    "questions_for_founders": ("questions_for_founders", 5000, None),
    "status": ("status", 40, Prospect.Status.values),
    "next_action": ("next_action", 250, None),
    "comments": ("comments", 5000, None),
    "meeting_timezone": ("meeting_timezone", 80, None),
    "meeting_participants": ("meeting_participants", 500, None),
}

API_PROSPECT_BOOLEAN_FIELDS = {
    "founder_escalation_required": "founder_escalation_required",
    "prospect_sent": "prospect_sent",
    "is_not_eligible": "is_not_eligible",
}

API_PROSPECT_DATE_FIELDS = {
    "next_action_date": "next_action_date",
}

API_PROSPECT_DATETIME_FIELDS = {
    "meeting_scheduled_at": "meeting_scheduled_at",
}

PROSPECT_WORKFLOW_FIELDS = {
    "prospect-sent": ("prospect_sent",),
    "founder-linkedin": (
        "founder_account",
        "linkedin_connection_status",
        "personalization_note",
        "founder_escalation_required",
        "founder_escalation_notes",
    ),
    "interest-handoff": (
        "material_shared",
        "interest_signal",
        "questions_for_founders",
    ),
    "follow-up": (
        "status",
        "next_action",
        "next_action_date",
        "comments",
        "meeting_scheduled_at",
        "meeting_timezone",
        "meeting_participants",
    ),
}

PROSPECT_WORKFLOW_MESSAGES = {
    "prospect-sent": "Prospect sent status updated.",
    "founder-linkedin": "Founder LinkedIn details updated.",
    "interest-handoff": "Interest and handoff details updated.",
    "follow-up": "Follow-up details updated.",
}

LINKEDIN_ONLY_PROSPECT_FIELDS = {
    "founder_account",
    "linkedin_connection_status",
    "personalization_note",
    "founder_escalation_required",
    "founder_escalation_notes",
}

API_OUTREACH_FIELDS = {
    "activity_type",
    "medium",
    "outreach_date",
    "response",
}
API_OUTREACH_CREATE_REQUIRED_FIELDS = {
    "activity_type",
    "medium",
    "outreach_date",
}


def _json_error(message, status, *, errors=None):
    payload = {"error": message}
    if errors:
        payload["field_errors"] = errors
    response = JsonResponse(payload, status=status)
    response["Cache-Control"] = "no-store"
    return response


def _read_json_object(request):
    try:
        payload = json.loads(request.body or b"{}")
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise ValueError("Request body must be valid JSON.") from error
    if not isinstance(payload, dict):
        raise ValueError("Request body must be a JSON object.")
    return payload


def api_token_required(view):
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        authorization = request.headers.get("Authorization", "")
        scheme, separator, raw_token = authorization.partition(" ")
        if not separator or scheme.lower() != "bearer" or not raw_token.strip():
            return _json_error("Provide an access token as Authorization: Bearer <token>.", 401)
        try:
            request.api_user = authenticate_access_token(raw_token.strip())
        except ApiTokenError as error:
            return _json_error(str(error), 401)
        response = view(request, *args, **kwargs)
        response["Cache-Control"] = "no-store"
        return response

    return wrapped


def _display_name(user):
    return user.get_full_name() or user.email or user.username


def _company_details(records):
    records = list(records)
    primary = records[0]

    def first_value(model_field):
        return next((getattr(record, model_field) for record in records if getattr(record, model_field)), "")

    details = {
        "id": str(primary.company_id),
        "name": first_value("company_name"),
        "location": first_value("location"),
        "url": first_value("website"),
    }
    for api_field, (model_field, _max_length) in API_COMPANY_FIELDS.items():
        details[api_field] = first_value(model_field)
    details["workstreams"] = sorted({record.workstream for record in records})
    details["prospects"] = sorted(
        (
            {
                "id": record.pk,
                "workstream": record.workstream,
            }
            for record in records
        ),
        key=lambda item: (item["workstream"], item["id"]),
    )
    return details


def _validate_company_changes(payload):
    unknown_fields = sorted(set(payload) - set(API_COMPANY_FIELDS))
    if unknown_fields:
        hint = " Use eligibility_evidence for eligibility status or reason." if {
            "eligibility_status",
            "eligibility_reason",
        }.intersection(unknown_fields) else ""
        raise ValueError(f"Unsupported field(s): {', '.join(unknown_fields)}.{hint}")
    if not payload:
        raise ValueError("Provide at least one company field to update.")

    cleaned = {}
    errors = {}
    for api_field, value in payload.items():
        if value is None:
            value = ""
        if not isinstance(value, str):
            errors[api_field] = "Use a string or null value."
            continue
        value = value.strip()
        max_length = API_COMPANY_FIELDS[api_field][1]
        if len(value) > max_length:
            errors[api_field] = f"Use no more than {max_length} characters."
            continue
        try:
            if api_field == "email" and value:
                EmailValidator()(value)
            elif api_field == "linkedin_url" and value:
                URLValidator(schemes=["http", "https"])(value)
        except ValidationError as error:
            errors[api_field] = error.messages[0]
            continue
        cleaned[api_field] = value
    if errors:
        validation_error = ValueError("One or more company fields are invalid.")
        validation_error.field_errors = errors
        raise validation_error
    return cleaned


def _serialize_api_value(value):
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def _prospect_details(prospect, *, fields=None):
    owner = None
    if prospect.owner_id:
        owner = {
            "name": _display_name(prospect.owner),
            "email": prospect.owner.email,
        }
    details = {
        "id": prospect.pk,
        "company_id": str(prospect.company_id),
        "company_name": prospect.company_name,
        "workstream": prospect.workstream,
        "owner": owner,
    }
    selected_fields = set(fields) if fields is not None else None
    for api_field, (model_field, _max_length, _choices) in API_PROSPECT_STRING_FIELDS.items():
        if selected_fields is None or api_field in selected_fields:
            details[api_field] = getattr(prospect, model_field)
    for api_field, model_field in API_PROSPECT_BOOLEAN_FIELDS.items():
        if selected_fields is None or api_field in selected_fields:
            details[api_field] = getattr(prospect, model_field)
    for api_field, model_field in API_PROSPECT_DATE_FIELDS.items():
        if selected_fields is None or api_field in selected_fields:
            details[api_field] = _serialize_api_value(getattr(prospect, model_field))
    for api_field, model_field in API_PROSPECT_DATETIME_FIELDS.items():
        if selected_fields is None or api_field in selected_fields:
            details[api_field] = _serialize_api_value(getattr(prospect, model_field))
    return details


def _validate_prospect_changes(payload, *, allowed_fields=None):
    supported_fields = (
        set(API_PROSPECT_STRING_FIELDS)
        | set(API_PROSPECT_BOOLEAN_FIELDS)
        | set(API_PROSPECT_DATE_FIELDS)
        | set(API_PROSPECT_DATETIME_FIELDS)
    )
    if allowed_fields is not None:
        supported_fields &= set(allowed_fields)
    unknown_fields = sorted(set(payload) - supported_fields)
    if unknown_fields:
        raise ValueError(f"Unsupported field(s): {', '.join(unknown_fields)}.")
    if not payload:
        raise ValueError("Provide at least one prospect field to update.")

    cleaned = {}
    errors = {}
    for api_field, value in payload.items():
        if api_field in API_PROSPECT_BOOLEAN_FIELDS:
            if not isinstance(value, bool):
                errors[api_field] = "Use a JSON boolean: true or false."
                continue
            cleaned[api_field] = value
            continue

        if api_field in API_PROSPECT_DATE_FIELDS:
            if value is None or value == "":
                cleaned[api_field] = None
                continue
            if not isinstance(value, str):
                errors[api_field] = "Use an ISO date string in YYYY-MM-DD format or null."
                continue
            try:
                cleaned[api_field] = date.fromisoformat(value)
            except ValueError:
                errors[api_field] = "Use an ISO date string in YYYY-MM-DD format."
            continue

        if api_field in API_PROSPECT_DATETIME_FIELDS:
            if value is None or value == "":
                cleaned[api_field] = None
                continue
            if not isinstance(value, str):
                errors[api_field] = "Use an ISO 8601 date-time string with a UTC offset or null."
                continue
            parsed_value = parse_datetime(value)
            if parsed_value is None or timezone.is_naive(parsed_value):
                errors[api_field] = (
                    "Use an ISO 8601 date-time with a UTC offset, for example "
                    "2026-09-15T10:30:00-04:00."
                )
                continue
            cleaned[api_field] = parsed_value
            continue

        if value is None:
            value = ""
        if not isinstance(value, str):
            errors[api_field] = "Use a string or null value."
            continue
        value = value.strip()
        _model_field, max_length, choices = API_PROSPECT_STRING_FIELDS[api_field]
        if len(value) > max_length:
            errors[api_field] = f"Use no more than {max_length} characters."
            continue
        if value and choices and value not in choices:
            errors[api_field] = f"Choose one of: {', '.join(choices)}."
            continue
        cleaned[api_field] = value

    if errors:
        validation_error = ValueError("One or more prospect fields are invalid.")
        validation_error.field_errors = errors
        raise validation_error
    return cleaned


@never_cache
@login_required
@require_http_methods(["GET", "POST"])
def api_access(request):
    token_pair = issue_token_pair(request.user) if request.method == "POST" else None
    response = render(
        request,
        "outreach/api_access.html",
        {
            "token_pair": token_pair,
            "active_tokens": request.user.api_access_tokens.filter(
                revoked_at__isnull=True
            ),
            "company_fields": API_COMPANY_FIELDS.keys(),
            "workflow_api_docs": [
                API_DOCUMENTATION[slug] for slug in API_DOCUMENTATION_ORDER
            ],
        },
    )
    response["Cache-Control"] = "no-store"
    return response


@never_cache
@login_required
@require_http_methods(["GET"])
def api_documentation_page(request, slug):
    documentation = API_DOCUMENTATION.get(slug)
    if documentation is None:
        raise Http404("API documentation page was not found.")
    response = render(
        request,
        "outreach/api_documentation.html",
        {
            "documentation": documentation,
            "workflow_api_docs": [
                API_DOCUMENTATION[item] for item in API_DOCUMENTATION_ORDER
            ],
        },
    )
    response["Cache-Control"] = "private, no-store"
    return response


@login_required
@require_POST
def revoke_access_token(request, pk):
    token = get_object_or_404(
        ApiAccessToken,
        pk=pk,
        user=request.user,
        revoked_at__isnull=True,
    )
    token.revoked_at = timezone.now()
    token.save(update_fields=["revoked_at"])
    messages.success(request, f"API token {token.token_prefix}… was revoked.")
    return redirect("api_access")


@csrf_exempt
@require_POST
def token_refresh(request):
    try:
        payload = _read_json_object(request)
    except ValueError as error:
        return _json_error(str(error), 400)
    raw_token = payload.get("refresh_token")
    if not isinstance(raw_token, str) or not raw_token.strip():
        return _json_error("Provide refresh_token in the JSON request body.", 400)
    try:
        token_pair = rotate_refresh_token(raw_token.strip())
    except ApiTokenError as error:
        return _json_error(str(error), 401)
    response = JsonResponse(token_pair)
    response["Cache-Control"] = "no-store"
    return response


@csrf_exempt
@api_token_required
@require_http_methods(["GET", "PATCH"])
def company_detail(request, company_id):
    if request.method == "GET":
        records = list(
            Prospect.objects.filter(company_id=company_id)
            .select_related("company")
            .order_by("-updated_at", "pk")
        )
        if not records:
            return _json_error("Company ID was not found.", 404)
        if not is_manager(request.api_user) and not any(
            record.owner_id == request.api_user.id for record in records
        ):
            return _json_error("Claim this prospect before viewing its company details via API.", 403)
        return JsonResponse({"company": _company_details(records)})

    try:
        payload = _read_json_object(request)
        cleaned_changes = _validate_company_changes(payload)
    except ValueError as error:
        return _json_error(
            str(error),
            400,
            errors=getattr(error, "field_errors", None),
        )

    with transaction.atomic():
        try:
            company = CompanyIdentity.objects.get(pk=company_id)
        except CompanyIdentity.DoesNotExist:
            return _json_error("Company ID was not found.", 404)
        records = list(
            Prospect.objects.select_for_update()
            .filter(company=company)
            .order_by("-updated_at", "pk")
        )
        if not is_manager(request.api_user) and not any(
            record.owner_id == request.api_user.id for record in records
        ):
            return _json_error("Claim this prospect before modifying its company details.", 403)

        previous_values = {}
        for api_field, (model_field, _max_length) in API_COMPANY_FIELDS.items():
            if api_field in cleaned_changes:
                previous_values[api_field] = sorted(
                    {getattr(record, model_field) for record in records}
                )

        model_changes = {
            API_COMPANY_FIELDS[api_field][0]: value
            for api_field, value in cleaned_changes.items()
        }
        try:
            for record in records:
                for model_field, value in model_changes.items():
                    setattr(record, model_field, value)
                record.full_clean()
        except ValidationError as error:
            return _json_error(
                "The update would leave one or more prospect records invalid.",
                400,
                errors=getattr(error, "message_dict", {"company": error.messages}),
            )

        update_fields = [*model_changes, "updated_at"]
        for record in records:
            record.save(update_fields=update_fields)
        CompanyUpdateAudit.objects.create(
            company=company,
            modified_by=request.api_user,
            previous_values=previous_values,
            changed_values=cleaned_changes,
        )

    refreshed_records = Prospect.objects.filter(company_id=company_id).order_by("-updated_at", "pk")
    return JsonResponse(
        {
            "message": "Company details updated.",
            "modified_by": {
                "name": _display_name(request.api_user),
                "email": request.api_user.email,
            },
            "company": _company_details(refreshed_records),
        }
    )


def _prospect_model_field(api_field):
    if api_field in API_PROSPECT_STRING_FIELDS:
        return API_PROSPECT_STRING_FIELDS[api_field][0]
    if api_field in API_PROSPECT_BOOLEAN_FIELDS:
        return API_PROSPECT_BOOLEAN_FIELDS[api_field]
    if api_field in API_PROSPECT_DATE_FIELDS:
        return API_PROSPECT_DATE_FIELDS[api_field]
    return API_PROSPECT_DATETIME_FIELDS[api_field]


def _prospect_response_is_recorded(prospect):
    later_response_stages = {
        Prospect.Stage.RESPONDED,
        Prospect.Stage.INTERESTED,
        Prospect.Stage.FOUNDER_MEETING_BOOKED,
        Prospect.Stage.MEETING_COMPLETED,
        Prospect.Stage.CLOSED,
    }
    return (
        bool(prospect.interest_signal.strip())
        or prospect.stage in later_response_stages
        or prospect.outreaches.exclude(response="").exists()
    )


def _validate_founder_escalation_workflow(prospect, cleaned_changes):
    if not cleaned_changes.get("founder_escalation_required"):
        return None
    final_connection_status = cleaned_changes.get(
        "linkedin_connection_status",
        prospect.linkedin_connection_status,
    )
    requirement_errors = []
    if final_connection_status != Prospect.LinkedInConnectionStatus.ACCEPTED:
        requirement_errors.append(
            "Mark the LinkedIn invitation as accepted before requesting founder escalation."
        )
    if not _prospect_response_is_recorded(prospect):
        requirement_errors.append(
            "Record the prospect response or interest signal before requesting founder escalation."
        )
    if requirement_errors:
        return _json_error(
            "Founder escalation is available only after the invitation is accepted and a response is recorded.",
            400,
            errors={
                "founder_escalation_required": " ".join(requirement_errors)
            },
        )
    return None


def _prospect_workflow_api(
    request,
    prospect_id,
    *,
    allowed_fields=None,
    message="Prospect workflow details updated.",
    validate_founder_escalation=False,
):
    response_fields = tuple(allowed_fields) if allowed_fields is not None else None
    if request.method == "GET":
        try:
            prospect = Prospect.objects.select_related("owner").get(pk=prospect_id)
        except Prospect.DoesNotExist:
            return _json_error("Prospect ID was not found.", 404)
        if not is_manager(request.api_user) and prospect.owner_id != request.api_user.id:
            return _json_error("Claim this prospect before viewing its workflow details via API.", 403)
        return JsonResponse(
            {"prospect": _prospect_details(prospect, fields=response_fields)}
        )

    try:
        payload = _read_json_object(request)
        cleaned_changes = _validate_prospect_changes(
            payload,
            allowed_fields=allowed_fields,
        )
    except ValueError as error:
        return _json_error(
            str(error),
            400,
            errors=getattr(error, "field_errors", None),
        )

    with transaction.atomic():
        try:
            # Do not join nullable owner while locking the prospect row.
            prospect = Prospect.objects.select_for_update().get(pk=prospect_id)
        except Prospect.DoesNotExist:
            return _json_error("Prospect ID was not found.", 404)
        if not is_manager(request.api_user) and prospect.owner_id != request.api_user.id:
            return _json_error("Claim this prospect before modifying its workflow details.", 403)

        invalid_linkedin_fields = sorted(
            set(cleaned_changes).intersection(LINKEDIN_ONLY_PROSPECT_FIELDS)
        )
        if (
            invalid_linkedin_fields
            and prospect.workstream != Prospect.Workstream.LINKEDIN_OUTREACH
        ):
            return _json_error(
                "Founder LinkedIn fields can only be updated for a LinkedIn outreach prospect.",
                400,
                errors={
                    field: "This field is only available in the LinkedIn outreach workstream."
                    for field in invalid_linkedin_fields
                },
            )

        if validate_founder_escalation:
            validation_response = _validate_founder_escalation_workflow(
                prospect,
                cleaned_changes,
            )
            if validation_response is not None:
                return validation_response

        previous_values = {}
        model_changes = {}
        original_stage = prospect.stage
        for api_field, value in cleaned_changes.items():
            model_field = _prospect_model_field(api_field)
            previous_values[api_field] = _serialize_api_value(
                getattr(prospect, model_field)
            )
            model_changes[model_field] = value
            setattr(prospect, model_field, value)

        if "status" in cleaned_changes:
            prospect.sync_stage_from_status()

        try:
            prospect.full_clean()
        except ValidationError as error:
            return _json_error(
                "The update would leave the prospect record invalid.",
                400,
                errors=getattr(error, "message_dict", {"prospect": error.messages}),
            )

        update_fields = list(model_changes)
        if prospect.stage != original_stage:
            update_fields.append("stage")
        update_fields.append("updated_at")
        prospect.save(update_fields=update_fields)
        ProspectUpdateAudit.objects.create(
            prospect=prospect,
            modified_by=request.api_user,
            previous_values=previous_values,
            changed_values={
                field: _serialize_api_value(value)
                for field, value in cleaned_changes.items()
            },
        )

    refreshed_prospect = Prospect.objects.select_related("owner").get(pk=prospect_id)
    return JsonResponse(
        {
            "message": message,
            "modified_by": {
                "name": _display_name(request.api_user),
                "email": request.api_user.email,
            },
            "prospect": _prospect_details(
                refreshed_prospect,
                fields=response_fields,
            ),
        }
    )


@csrf_exempt
@api_token_required
@require_http_methods(["GET", "PATCH"])
def prospect_detail(request, prospect_id):
    return _prospect_workflow_api(request, prospect_id)


@csrf_exempt
@api_token_required
@require_http_methods(["GET", "PATCH"])
def prospect_sent(request, prospect_id):
    return _prospect_workflow_api(
        request,
        prospect_id,
        allowed_fields=PROSPECT_WORKFLOW_FIELDS["prospect-sent"],
        message=PROSPECT_WORKFLOW_MESSAGES["prospect-sent"],
    )


@csrf_exempt
@api_token_required
@require_http_methods(["GET", "PATCH"])
def founder_linkedin(request, prospect_id):
    return _prospect_workflow_api(
        request,
        prospect_id,
        allowed_fields=PROSPECT_WORKFLOW_FIELDS["founder-linkedin"],
        message=PROSPECT_WORKFLOW_MESSAGES["founder-linkedin"],
        validate_founder_escalation=True,
    )


@csrf_exempt
@api_token_required
@require_http_methods(["GET", "PATCH"])
def interest_handoff(request, prospect_id):
    return _prospect_workflow_api(
        request,
        prospect_id,
        allowed_fields=PROSPECT_WORKFLOW_FIELDS["interest-handoff"],
        message=PROSPECT_WORKFLOW_MESSAGES["interest-handoff"],
    )


@csrf_exempt
@api_token_required
@require_http_methods(["GET", "PATCH"])
def follow_up(request, prospect_id):
    return _prospect_workflow_api(
        request,
        prospect_id,
        allowed_fields=PROSPECT_WORKFLOW_FIELDS["follow-up"],
        message=PROSPECT_WORKFLOW_MESSAGES["follow-up"],
    )


def _outreach_details(outreach):
    return {
        "id": outreach.pk,
        "prospect_id": outreach.prospect_id,
        "company_id": str(outreach.prospect.company_id),
        "company_name": outreach.prospect.company_name,
        "sequence_number": outreach.sequence_number,
        "activity_type": outreach.activity_type,
        "activity_type_display": outreach.get_activity_type_display(),
        "medium": outreach.medium,
        "medium_display": outreach.get_medium_display(),
        "outreach_date": outreach.outreach_date.isoformat(),
        "response": outreach.response,
        "recorded_by": {
            "name": _display_name(outreach.recorded_by),
            "email": outreach.recorded_by.email,
        },
        "created_at": outreach.created_at.isoformat(),
        "updated_at": outreach.updated_at.isoformat(),
    }


def _prepare_outreach_form_data(payload, *, instance=None):
    unknown_fields = sorted(set(payload) - API_OUTREACH_FIELDS)
    if unknown_fields:
        raise ValueError(f"Unsupported field(s): {', '.join(unknown_fields)}.")
    if not payload:
        raise ValueError("Provide at least one outreach field.")

    errors = {}
    if instance is None:
        for field in sorted(API_OUTREACH_CREATE_REQUIRED_FIELDS - set(payload)):
            errors[field] = "This field is required when creating outreach history."

    cleaned_payload = {}
    for field, value in payload.items():
        if field == "response" and value is None:
            value = ""
        if not isinstance(value, str):
            errors[field] = (
                "Use a string or null value."
                if field == "response"
                else "Use a string value."
            )
            continue
        cleaned_payload[field] = value

    if errors:
        validation_error = ValueError("One or more outreach fields are invalid.")
        validation_error.field_errors = errors
        raise validation_error

    if instance is None:
        return cleaned_payload, cleaned_payload.copy()

    form_data = {
        "activity_type": instance.activity_type,
        "medium": instance.medium,
        "outreach_date": instance.outreach_date.isoformat(),
        "response": instance.response,
    }
    form_data.update(cleaned_payload)
    return cleaned_payload, form_data


def _outreach_form_errors(form, *, prospect, submitted_fields):
    errors = {
        ("non_field_errors" if field == "__all__" else field): " ".join(
            item["message"] for item in field_errors
        )
        for field, field_errors in form.errors.get_json_data().items()
    }
    if (
        prospect.workstream == Prospect.Workstream.LINKEDIN_OUTREACH
        and "medium" in submitted_fields
        and submitted_fields["medium"] != Outreach.Medium.LINKEDIN
    ):
        errors["medium"] = "LinkedIn outreach prospects must use the linkedin medium."
    return errors


def _api_user_can_access_prospect(user, prospect):
    return is_manager(user) or prospect.owner_id == user.id


@csrf_exempt
@api_token_required
@require_http_methods(["GET", "POST"])
def prospect_outreach_list(request, prospect_id):
    if request.method == "GET":
        try:
            prospect = Prospect.objects.select_related("owner").get(pk=prospect_id)
        except Prospect.DoesNotExist:
            return _json_error("Prospect ID was not found.", 404)
        if not _api_user_can_access_prospect(request.api_user, prospect):
            return _json_error(
                "Claim this prospect before viewing its outreach history via API.",
                403,
            )
        outreaches = prospect.outreaches.select_related("recorded_by").all()
        return JsonResponse(
            {
                "prospect": _prospect_details(prospect, fields=()),
                "count": len(outreaches),
                "maximum": 5,
                "outreaches": [_outreach_details(outreach) for outreach in outreaches],
            }
        )

    try:
        payload = _read_json_object(request)
        submitted_fields, form_data = _prepare_outreach_form_data(payload)
    except ValueError as error:
        return _json_error(
            str(error),
            400,
            errors=getattr(error, "field_errors", None),
        )

    with transaction.atomic():
        try:
            # Lock only the prospect row. Joining nullable owner here causes the
            # same PostgreSQL FOR UPDATE error fixed in the browser add flow.
            prospect = Prospect.objects.select_for_update().get(pk=prospect_id)
        except Prospect.DoesNotExist:
            return _json_error("Prospect ID was not found.", 404)
        if not _api_user_can_access_prospect(request.api_user, prospect):
            return _json_error(
                "Claim this prospect before adding outreach history via API.",
                403,
            )
        if prospect.stage == Prospect.Stage.RESEARCH:
            return _json_error(
                "Complete the required contact and qualification research before recording outreach.",
                400,
            )

        existing_numbers = list(
            prospect.outreaches.order_by("sequence_number").values_list(
                "sequence_number",
                flat=True,
            )
        )
        if len(existing_numbers) >= 5:
            return _json_error(
                "This prospect already has the maximum of five outreach records.",
                409,
            )

        form = OutreachForm(form_data, prospect=prospect)
        form.is_valid()
        field_errors = _outreach_form_errors(
            form,
            prospect=prospect,
            submitted_fields=submitted_fields,
        )
        if field_errors:
            return _json_error(
                "One or more outreach fields are invalid.",
                400,
                errors=field_errors,
            )

        outreach = form.save(commit=False)
        outreach.prospect = prospect
        outreach.recorded_by = request.api_user
        outreach.sequence_number = next(
            number for number in range(1, 6) if number not in existing_numbers
        )
        try:
            with transaction.atomic():
                outreach.save()
        except IntegrityError:
            return _json_error(
                "Another outreach was added at the same time. Please try again.",
                409,
            )
        sync_prospect_from_outreach(prospect, outreach)

    created_outreach = Outreach.objects.select_related(
        "prospect",
        "recorded_by",
    ).get(pk=outreach.pk)
    return JsonResponse(
        {
            "message": f"Outreach {outreach.sequence_number} was recorded.",
            "outreach": _outreach_details(created_outreach),
        },
        status=201,
    )


@csrf_exempt
@api_token_required
@require_http_methods(["GET", "PATCH"])
def outreach_detail(request, outreach_id):
    if request.method == "GET":
        try:
            outreach = Outreach.objects.select_related(
                "prospect",
                "prospect__owner",
                "recorded_by",
            ).get(pk=outreach_id)
        except Outreach.DoesNotExist:
            return _json_error("Outreach ID was not found.", 404)
        if not _api_user_can_access_prospect(request.api_user, outreach.prospect):
            return _json_error(
                "You must own this outreach's prospect before viewing its history via API.",
                403,
            )
        return JsonResponse({"outreach": _outreach_details(outreach)})

    try:
        payload = _read_json_object(request)
    except ValueError as error:
        return _json_error(str(error), 400)

    try:
        prospect_id = Outreach.objects.values_list("prospect_id", flat=True).get(
            pk=outreach_id
        )
    except Outreach.DoesNotExist:
        return _json_error("Outreach ID was not found.", 404)

    with transaction.atomic():
        try:
            prospect = Prospect.objects.select_for_update().get(pk=prospect_id)
            outreach = Outreach.objects.select_for_update().get(
                pk=outreach_id,
                prospect_id=prospect_id,
            )
        except (Prospect.DoesNotExist, Outreach.DoesNotExist):
            return _json_error("Outreach ID was not found.", 404)
        if not _api_user_can_access_prospect(request.api_user, prospect):
            return _json_error(
                "You must own this outreach's prospect before modifying its history via API.",
                403,
            )

        try:
            submitted_fields, form_data = _prepare_outreach_form_data(
                payload,
                instance=outreach,
            )
        except ValueError as error:
            return _json_error(
                str(error),
                400,
                errors=getattr(error, "field_errors", None),
            )

        # ModelForm validation writes cleaned values onto its instance, so take
        # the audit snapshot before constructing and validating the form.
        previous_values = {
            field: _serialize_api_value(getattr(outreach, field))
            for field in submitted_fields
        }
        form = OutreachForm(form_data, instance=outreach, prospect=prospect)
        form.is_valid()
        field_errors = _outreach_form_errors(
            form,
            prospect=prospect,
            submitted_fields=submitted_fields,
        )
        if field_errors:
            return _json_error(
                "One or more outreach fields are invalid.",
                400,
                errors=field_errors,
            )

        outreach = form.save()
        sync_prospect_from_outreach(prospect, outreach)
        changed_values = {
            field: _serialize_api_value(getattr(outreach, field))
            for field in submitted_fields
        }
        OutreachUpdateAudit.objects.create(
            outreach=outreach,
            modified_by=request.api_user,
            previous_values=previous_values,
            changed_values=changed_values,
        )

    updated_outreach = Outreach.objects.select_related(
        "prospect",
        "recorded_by",
    ).get(pk=outreach_id)
    return JsonResponse(
        {
            "message": f"Outreach {outreach.sequence_number} was updated.",
            "modified_by": {
                "name": _display_name(request.api_user),
                "email": request.api_user.email,
            },
            "outreach": _outreach_details(updated_outreach),
        }
    )


@csrf_exempt
@api_token_required
@require_POST
def company_claim(request, company_id):
    user = request.api_user
    try:
        role = user.profile.role
    except Profile.DoesNotExist:
        return _json_error("The token user does not have an outreach class.", 403)
    if role not in Prospect.Workstream.values:
        return _json_error("Managers and system administrators cannot claim prospects.", 403)

    with transaction.atomic():
        # Keep the nullable owner out of this locking query. PostgreSQL rejects
        # FOR UPDATE when it applies to the nullable side of an outer join.
        prospects = list(
            Prospect.objects.select_for_update()
            .filter(company_id=company_id, workstream=role)
            .order_by("pk")
        )
        if not prospects:
            return _json_error("No prospect exists for this company in your outreach class.", 404)
        if len(prospects) > 1:
            return _json_error("Multiple prospects exist for this company in your outreach class.", 409)

        prospect = prospects[0]
        if prospect.owner_id is None:
            prospect.owner = user
            prospect.save(update_fields=["owner", "updated_at"])
            return JsonResponse(
                {
                    "message": f"{prospect.company_name} is now claimed by {_display_name(user)}.",
                    "company_id": str(prospect.company_id),
                    "prospect_id": prospect.pk,
                    "claimed_by": _display_name(user),
                }
            )
        if prospect.owner_id == user.id:
            return JsonResponse(
                {
                    "message": f"This prospect is already claimed by {_display_name(user)}.",
                    "company_id": str(prospect.company_id),
                    "prospect_id": prospect.pk,
                    "claimed_by": _display_name(user),
                }
            )

        owner_name = _display_name(prospect.owner)
        return JsonResponse(
            {
                "error": f"This prospect is already claimed by {owner_name}.",
                "company_id": str(prospect.company_id),
                "prospect_id": prospect.pk,
                "claimed_by": owner_name,
            },
            status=409,
        )
