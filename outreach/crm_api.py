"""Versioned CRM APIs. Existing outreach API contracts remain unchanged."""

from functools import wraps
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.forms.models import model_to_dict
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.csrf import csrf_exempt

from . import crm
from .api import _json_error, _read_json_object, api_token_required
from .crm_forms import HandoffForm, LeadUpdateForm, OpportunityForm, SubmissionForm
from .crm_views import filtered_records, ordered_records
from .models import Opportunity, Prospect
from .permissions import active_role, can_edit_lead, can_view_crm, is_manager


def endpoint(*methods):
    def decorate(view):
        @csrf_exempt
        @api_token_required
        @wraps(view)
        def wrapped(request, *args, **kwargs):
            if request.method not in methods:
                response = _json_error("Method not allowed.", 405)
                response["Allow"] = ", ".join(methods)
                return response
            try:
                return view(request, *args, **kwargs)
            except PermissionDenied as error:
                return _json_error(str(error) or "Access denied.", 403)
            except Http404:
                return _json_error("Record not found.", 404)
            except ValueError as error:
                return _json_error(str(error), 400)

        return wrapped

    return decorate


def payload(request, allowed):
    data = _read_json_object(request)
    unknown = sorted(set(data) - set(allowed))
    if unknown:
        raise ValueError("Unsupported fields: " + ", ".join(unknown))
    for key, value in data.items():
        if key == "response_confirmed":
            if not isinstance(value, bool):
                raise ValueError("response_confirmed must be a JSON boolean.")
        elif key in {"assigned_to", "version", "probability", "next_action_owner"}:
            if key == "next_action_owner" and value is None:
                continue
            if type(value) is not int:
                raise ValueError(f"{key} must be a JSON integer.")
        elif key == "estimated_value":
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, (str, int, float))
            ):
                raise ValueError(
                    "estimated_value must be a decimal string, number, or null."
                )
        elif value is not None and not isinstance(value, str):
            raise ValueError(f"{key} must be a string or null.")
    return data


def form_error(form):
    stale = any(
        "changed while you were editing" in message
        for message in form.non_field_errors()
    )
    return _json_error(
        "Record changed. Reload before saving." if stale else "Validation failed.",
        409 if stale else 400,
        errors=form.errors.get_json_data(),
    )


def person(user):
    if user is None:
        return None
    return {
        "id": user.pk,
        "name": user.get_full_name() or user.email,
        "active": user.is_active,
    }


def lead_data(lead, user):
    prospect = lead.prospect
    fields = [
        "id",
        "source_channel",
        "escalated_at",
        "historical",
        "approval_status",
        "approved_at",
        "context",
        "status",
        "next_action",
        "next_action_date",
        "discovery_at",
        "discovery_timezone",
        "discovery_outcome",
        "drop_reason",
        "version",
        "created_at",
        "updated_at",
    ]
    data = {field: getattr(lead, field) for field in fields}
    data.update(
        {
            "assigned_to": person(lead.assigned_to),
            "approved_by": person(lead.approved_by),
            "next_action_owner": person(lead.next_action_owner),
            "escalated_by": person(lead.escalated_by),
            "can_edit": can_edit_lead(user, lead),
            "prospect": {
                field: getattr(prospect, field)
                for field in [
                    "id",
                    "company_id",
                    "company_name",
                    "website",
                    "location",
                    "contact_name",
                    "contact_title",
                    "contact_email",
                    "contact_phone",
                    "contact_linkedin_url",
                    "owner_id",
                    "lead_source",
                    "first_contact_date",
                    "is_test",
                ]
            },
            "opportunity_id": getattr(getattr(lead, "opportunity", None), "pk", None),
        }
    )
    return data


def opportunity_data(record, user):
    fields = [
        "id",
        *OpportunityForm.Meta.fields,
        "closed_at",
        "version",
        "created_at",
        "updated_at",
    ]
    return {
        **{field: getattr(record, field + "_id" if field == "next_action_owner" else field) for field in fields},
        "lead": lead_data(record.escalation, user),
    }


def listing(request, rows, serialize, pipeline=False):
    form, rows = filtered_records(rows, request.GET, pipeline=pipeline)
    if form.is_bound and not form.is_valid():
        return form_error(form)
    page = Paginator(ordered_records(rows, form), 25).get_page(request.GET.get("page"))
    return JsonResponse(
        {
            "count": page.paginator.count,
            "page": page.number,
            "pages": page.paginator.num_pages,
            "results": [serialize(record, request.api_user) for record in page],
        }
    )


@endpoint("GET", "POST")
def leads(request):
    if request.method == "GET":
        return listing(
            request,
            crm.visible_leads(request.api_user).select_related("opportunity"),
            lead_data,
        )
    data = payload(request, [*SubmissionForm.Meta.fields, "assigned_to", "context"])
    if data.get("workstream") is not None and data["workstream"] not in Prospect.Workstream.values:
        raise ValueError("Choose a valid legacy workstream.")
    if active_role(request.api_user) in Prospect.Workstream.values and data.get("workstream") not in {
        None,
        active_role(request.api_user),
    }:
        raise ValueError("workstream must match the submitting user's role.")
    form, lead = crm.submit_lead(request.api_user, data)
    return (
        JsonResponse(lead_data(lead, request.api_user), status=201)
        if lead
        else form_error(form)
    )


@endpoint("POST")
def handoff(request, pk):
    prospect = get_object_or_404(Prospect, pk=pk)
    form = HandoffForm(prospect=prospect)
    data = payload(request, form.fields)
    form, lead = crm.escalate_prospect(request.api_user, pk, data)
    return JsonResponse(lead_data(lead, request.api_user)) if lead else form_error(form)


def patch_data(request, record, form, fields):
    changes = payload(request, form.fields)
    if "version" not in changes:
        raise ValueError("version from the latest GET response is required for PATCH.")
    if set(changes) == {"version"}:
        raise ValueError("Provide at least one field to update.")
    # Fill omitted fields for partial updates. Only explicit version is accepted.
    data = model_to_dict(record, fields=fields)
    data.update(changes)
    return data


@endpoint("GET", "PATCH")
def lead_detail(request, pk):
    lead = get_object_or_404(crm.visible_leads(request.api_user), pk=pk)
    if request.method == "GET":
        return JsonResponse(lead_data(lead, request.api_user))
    crm.require_editor(request.api_user, lead)
    form = LeadUpdateForm(user=request.api_user, instance=lead)
    data = patch_data(request, lead, form, LeadUpdateForm.Meta.fields)
    # API timestamps must include an offset. Convert to the form's explicit zone.
    value = data.get("discovery_at")
    if value:
        try:
            value = parse_datetime(value) if isinstance(value, str) else value
            if not value or timezone.is_naive(value):
                raise ValueError(
                    "discovery_at must be an ISO 8601 timestamp with a UTC offset."
                )
            zone = ZoneInfo(data.get("discovery_timezone") or "")
            data["discovery_at"] = (
                timezone.localtime(value, zone).replace(tzinfo=None).isoformat()
            )
        except ZoneInfoNotFoundError:
            raise ValueError("A valid IANA discovery_timezone is required.")
    form, saved = crm.update_lead(request.api_user, pk, data)
    return (
        JsonResponse(lead_data(saved, request.api_user)) if saved else form_error(form)
    )


@endpoint("POST")
def convert(request, pk):
    lead = get_object_or_404(crm.visible_leads(request.api_user), pk=pk)
    crm.require_editor(request.api_user, lead)
    form = OpportunityForm(user=request.api_user, lead=lead)
    data = payload(request, form.fields)
    if is_manager(request.api_user):
        data.setdefault("assigned_to", lead.assigned_to_id)
    form, saved = crm.convert_lead(request.api_user, pk, data)
    return (
        JsonResponse(opportunity_data(saved, request.api_user))
        if saved
        else form_error(form)
    )


@endpoint("POST")
def comment(request, pk):
    data = payload(request, {"comment"})
    form, lead = crm.add_comment(request.api_user, pk, data)
    return (
        JsonResponse({"recorded": True, "lead_id": lead.pk}, status=201)
        if lead
        else form_error(form)
    )


@endpoint("GET")
def activities(request, pk):
    lead = get_object_or_404(crm.visible_leads(request.api_user), pk=pk)
    page = Paginator(lead.activities.select_related("actor"), 25).get_page(
        request.GET.get("page")
    )
    return JsonResponse(
        {
            "count": page.paginator.count,
            "page": page.number,
            "pages": page.paginator.num_pages,
            "results": [
                {
                    "id": event.pk,
                    "actor": person(event.actor),
                    "kind": event.kind,
                    "comment": event.comment,
                    "changes": event.changes,
                    "created_at": event.created_at,
                }
                for event in page
            ],
        }
    )


@endpoint("GET")
def opportunities(request):
    if not can_view_crm(request.api_user):
        raise PermissionDenied
    return listing(
        request,
        Opportunity.objects.select_related(
            "escalation__prospect",
            "escalation__assigned_to",
            "escalation__escalated_by",
        ),
        opportunity_data,
        pipeline=True,
    )


@endpoint("GET", "PATCH")
def opportunity_detail(request, pk):
    if not can_view_crm(request.api_user):
        raise PermissionDenied
    record = get_object_or_404(Opportunity.objects.select_related("escalation"), pk=pk)
    if request.method == "GET":
        return JsonResponse(opportunity_data(record, request.api_user))
    crm.require_editor(request.api_user, record.escalation)
    form = OpportunityForm(
        instance=record, lead=record.escalation, user=request.api_user
    )
    data = patch_data(request, record, form, OpportunityForm.Meta.fields)
    if is_manager(request.api_user):
        data.setdefault("assigned_to", record.escalation.assigned_to_id)
    form, saved = crm.update_opportunity(request.api_user, pk, data)
    return (
        JsonResponse(opportunity_data(saved, request.api_user))
        if saved
        else form_error(form)
    )
