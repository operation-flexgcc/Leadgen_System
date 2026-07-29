import json
from functools import wraps

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.core.validators import EmailValidator, URLValidator
from django.db import transaction
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods, require_POST

from .api_tokens import (
    ApiTokenError,
    authenticate_access_token,
    issue_token_pair,
    rotate_refresh_token,
)
from .models import (
    ApiAccessToken,
    CompanyIdentity,
    CompanyUpdateAudit,
    Profile,
    Prospect,
    ProspectUpdateAudit,
)
from .permissions import is_manager


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
}

API_PROSPECT_BOOLEAN_FIELDS = {
    "founder_escalation_required": "founder_escalation_required",
    "prospect_sent": "prospect_sent",
    "is_not_eligible": "is_not_eligible",
}

LINKEDIN_ONLY_PROSPECT_FIELDS = {
    "founder_account",
    "linkedin_connection_status",
    "personalization_note",
    "founder_escalation_required",
    "founder_escalation_notes",
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


def _prospect_details(prospect):
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
    for api_field, (model_field, _max_length, _choices) in API_PROSPECT_STRING_FIELDS.items():
        details[api_field] = getattr(prospect, model_field)
    for api_field, model_field in API_PROSPECT_BOOLEAN_FIELDS.items():
        details[api_field] = getattr(prospect, model_field)
    return details


def _validate_prospect_changes(payload):
    supported_fields = set(API_PROSPECT_STRING_FIELDS) | set(API_PROSPECT_BOOLEAN_FIELDS)
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
        },
    )
    response["Cache-Control"] = "no-store"
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


@csrf_exempt
@api_token_required
@require_http_methods(["GET", "PATCH"])
def prospect_detail(request, prospect_id):
    if request.method == "GET":
        try:
            prospect = Prospect.objects.select_related("owner").get(pk=prospect_id)
        except Prospect.DoesNotExist:
            return _json_error("Prospect ID was not found.", 404)
        if not is_manager(request.api_user) and prospect.owner_id != request.api_user.id:
            return _json_error("Claim this prospect before viewing its workflow details via API.", 403)
        return JsonResponse({"prospect": _prospect_details(prospect)})

    try:
        payload = _read_json_object(request)
        cleaned_changes = _validate_prospect_changes(payload)
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

        previous_values = {}
        model_changes = {}
        for api_field, value in cleaned_changes.items():
            if api_field in API_PROSPECT_STRING_FIELDS:
                model_field = API_PROSPECT_STRING_FIELDS[api_field][0]
            else:
                model_field = API_PROSPECT_BOOLEAN_FIELDS[api_field]
            previous_values[api_field] = getattr(prospect, model_field)
            model_changes[model_field] = value
            setattr(prospect, model_field, value)

        try:
            prospect.full_clean()
        except ValidationError as error:
            return _json_error(
                "The update would leave the prospect record invalid.",
                400,
                errors=getattr(error, "message_dict", {"prospect": error.messages}),
            )

        prospect.save(update_fields=[*model_changes, "updated_at"])
        ProspectUpdateAudit.objects.create(
            prospect=prospect,
            modified_by=request.api_user,
            previous_values=previous_values,
            changed_values=cleaned_changes,
        )

    refreshed_prospect = Prospect.objects.select_related("owner").get(pk=prospect_id)
    return JsonResponse(
        {
            "message": "Prospect workflow details updated.",
            "modified_by": {
                "name": _display_name(request.api_user),
                "email": request.api_user.email,
            },
            "prospect": _prospect_details(refreshed_prospect),
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
