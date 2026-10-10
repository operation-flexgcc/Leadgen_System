from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db.models import Count, F, Q, Sum
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_http_methods, require_POST

from . import crm
from .crm_forms import (
    CRMFilterForm,
    CommentForm,
    HandoffForm,
    LeadUpdateForm,
    OpportunityForm,
    SubmissionForm,
)
from .models import Escalation, Opportunity, Prospect
from .permissions import (
    can_edit_lead,
    can_view_crm,
    is_manager,
    is_submission_user,
    is_system_admin,
)


def activity_page(request, lead):
    return Paginator(lead.activities.select_related("actor"), 25).get_page(
        request.GET.get("activity_page")
    )


@login_required
def api_guide(request):
    return render(
        request,
        "crm/api_guide.html",
        {"statuses": Escalation.Status.choices, "stages": Opportunity.Stage.choices},
    )


def filtered_records(rows, params, pipeline=False):
    form = CRMFilterForm(params or None, pipeline=pipeline)
    if not params:
        return form, rows
    if not form.is_valid():
        return form, rows.none()
    data = form.cleaned_data
    prefix = "escalation__" if pipeline else ""
    if data["search"]:
        term = data["search"].strip()
        query = (
            Q(**{prefix + "prospect__company_name__icontains": term})
            | Q(**{prefix + "prospect__contact_name__icontains": term})
            | Q(**{prefix + "prospect__contact_email__icontains": term})
        )
        if pipeline:
            query |= Q(name__icontains=term)
        rows = rows.filter(query)
    for field in ["assigned_to", "source_channel"]:
        if data[field]:
            rows = rows.filter(**{prefix + field: data[field]})
    if data["status"]:
        rows = rows.filter(**{"stage" if pipeline else "status": data["status"]})
    for key, lookup in [
        ("date_from", "escalated_at__date__gte"),
        ("date_to", "escalated_at__date__lte"),
    ]:
        if data[key]:
            rows = rows.filter(**{prefix + lookup: data[key]})
    if pipeline:
        if data.get("close_from"):
            rows = rows.filter(expected_close_date__gte=data["close_from"])
        if data.get("close_to"):
            rows = rows.filter(expected_close_date__lte=data["close_to"])
    due = data["due"]
    if due:
        rows = rows.exclude(
            **(
                {"stage__in": [Opportunity.Stage.WON, Opportunity.Stage.LOST]}
                if pipeline
                else {
                    "status__in": [
                        Escalation.Status.DROPPED,
                        Escalation.Status.CONVERTED,
                    ]
                }
            )
        )
        today = timezone.localdate()
        lookup, value = {
            "today": ("next_action_date", today),
            "overdue": ("next_action_date__lt", today),
            "upcoming": ("next_action_date__gt", today),
            "none": ("next_action_date__isnull", True),
        }[due]
        rows = rows.filter(**{lookup: value})
    return form, rows


def list_context(request, rows, pipeline=False):
    form, rows = filtered_records(rows, request.GET, pipeline=pipeline)
    active = rows.exclude(
        **(
            {"stage__in": [Opportunity.Stage.WON, Opportunity.Stage.LOST]}
            if pipeline
            else {
                "status__in": [Escalation.Status.DROPPED, Escalation.Status.CONVERTED]
            }
        )
    )
    today = timezone.localdate()
    summary = {
        "total": rows.count(),
        "active": active.count(),
        "overdue": active.filter(next_action_date__lt=today).count(),
        "today": active.filter(next_action_date=today).count(),
    }
    query = request.GET.copy()
    query.pop("page", None)
    context = {
        "filter_form": form,
        "page_obj": Paginator(
            rows.order_by(F("next_action_date").asc(nulls_last=True), "-pk"), 25
        ).get_page(request.GET.get("page")),
        "summary": summary,
        "query_string": query.urlencode(),
        "pipeline": pipeline,
        "today": today,
    }
    if pipeline:
        context["values_by_currency"] = (
            active.exclude(estimated_value__isnull=True)
            .values("currency")
            .annotate(total=Sum("estimated_value"), count=Count("pk"))
            .order_by("currency")
        )
    return context


@login_required
def lead_list(request):
    return render(
        request, "crm/list.html", list_context(request, crm.visible_leads(request.user))
    )


@login_required
def opportunity_list(request):
    if not can_view_crm(request.user):
        raise PermissionDenied
    rows = Opportunity.objects.select_related(
        "escalation__prospect", "escalation__assigned_to"
    )
    return render(request, "crm/list.html", list_context(request, rows, pipeline=True))


@login_required
@require_http_methods(["GET", "POST"])
def submit(request):
    if not (is_manager(request.user) or is_submission_user(request.user)):
        raise PermissionDenied
    form = SubmissionForm(user=request.user)
    if request.method == "POST":
        form, lead = crm.submit_lead(request.user, request.POST)
        if lead:
            messages.success(request, "Lead submitted to the account executive.")
            return redirect(lead)
    return render(
        request,
        "crm/form.html",
        {
            "form": form,
            "title": "Submit a lead",
            "intro": "Add a lead ready for executive follow-up. Earlier outreach takes place outside this system.",
            "submit_label": "Submit and escalate",
            "back_url": "crm_lead_list",
            "executive_required": True,
        },
    )


@login_required
@require_http_methods(["GET", "POST"])
def handoff(request, pk):
    from .views import get_scoped_prospect

    prospect = get_scoped_prospect(request.user, pk)
    if not (is_manager(request.user) or prospect.owner_id == request.user.pk):
        raise PermissionDenied("Claim this prospect before escalating it.")
    existing = Escalation.objects.filter(prospect=prospect).first()
    if existing:
        return redirect(existing)
    form = HandoffForm(
        prospect=prospect,
        initial={
            "context": prospect.interest_signal or prospect.founder_escalation_notes
        },
    )
    if request.method == "POST":
        form, lead = crm.escalate_prospect(request.user, pk, request.POST)
        if lead:
            messages.success(request, "The account executive handoff is recorded.")
            return redirect(lead)
    return render(
        request,
        "crm/form.html",
        {
            "form": form,
            "title": f"Escalate {prospect.company_name}",
            "intro": "Select the responsible executive and record the client reply and next-step context.",
            "submit_label": "Escalate lead",
            "back_url": "dashboard",
            "executive_required": True,
        },
    )


@login_required
@require_http_methods(["GET", "POST"])
def lead_detail(request, pk):
    lead = get_object_or_404(crm.visible_leads(request.user), pk=pk)
    editable = can_edit_lead(request.user, lead)
    converted = lead.status == Escalation.Status.CONVERTED
    form = (
        LeadUpdateForm(instance=lead, user=request.user)
        if editable and not converted
        else None
    )
    if request.method == "POST":
        form, saved = crm.update_lead(request.user, pk, request.POST)
        if saved:
            messages.success(request, "Lead updated.")
            return redirect(saved)
        # ModelForm validation can mutate its instance; display stored data.
        lead.refresh_from_db()
    return render(
        request,
        "crm/detail.html",
        {
            "lead": lead,
            "form": form,
            "can_edit": editable,
            "comment_form": CommentForm() if editable else None,
            "activities": activity_page(request, lead),
            "opportunity": Opportunity.objects.filter(escalation=lead).first(),
            "pipeline_detail": False,
        },
    )


@login_required
@require_http_methods(["GET", "POST"])
def convert(request, pk):
    lead = get_object_or_404(crm.visible_leads(request.user), pk=pk)
    crm.require_editor(request.user, lead)
    existing = Opportunity.objects.filter(escalation=lead).first()
    if existing:
        return redirect(existing)
    form = OpportunityForm(
        user=request.user,
        lead=lead,
        initial={
            "name": lead.prospect.company_name,
            "requirements": lead.context,
            "next_action": lead.next_action,
            "next_action_date": lead.next_action_date,
        },
    )
    if request.method == "POST":
        form, opportunity = crm.convert_lead(request.user, pk, request.POST)
        if opportunity:
            messages.success(request, "Lead moved to the sales pipeline.")
            return redirect(opportunity)
    return render(
        request,
        "crm/form.html",
        {
            "form": form,
            "title": "Move to sales pipeline",
            "intro": f"Create an opportunity for {lead.prospect.company_name}. The original handoff and history remain linked.",
            "submit_label": "Move to sales pipeline",
            "back_url": "crm_lead_list",
        },
    )


@login_required
@require_http_methods(["GET", "POST"])
def opportunity_detail(request, pk):
    if not can_view_crm(request.user):
        raise PermissionDenied
    record = get_object_or_404(
        Opportunity.objects.select_related(
            "escalation__prospect",
            "escalation__assigned_to",
            "escalation__escalated_by",
        ),
        pk=pk,
    )
    lead = record.escalation
    editable = can_edit_lead(request.user, lead)
    form = (
        OpportunityForm(instance=record, lead=lead, user=request.user)
        if editable
        else None
    )
    if request.method == "POST":
        form, saved = crm.update_opportunity(request.user, pk, request.POST)
        if saved:
            messages.success(request, "Sales opportunity updated.")
            return redirect(saved)
        record.refresh_from_db()
    return render(
        request,
        "crm/detail.html",
        {
            "lead": lead,
            "opportunity": record,
            "pipeline_detail": True,
            "form": form,
            "can_edit": editable,
            "comment_form": CommentForm() if editable else None,
            "activities": activity_page(request, lead),
        },
    )


@login_required
@require_POST
def comment(request, pk):
    form, lead = crm.add_comment(request.user, pk, request.POST)
    if lead:
        messages.success(request, "Comment added to the activity history.")
    else:
        messages.error(request, "Enter a comment of up to 5,000 characters.")
    return redirect("crm_lead_detail", pk=pk)


@login_required
def migration_review(request):
    if not is_system_admin(request.user):
        raise PermissionDenied
    candidates = crm.review_candidates()
    search = request.GET.get("search", "").strip()
    if search:
        candidates = candidates.filter(
            Q(company_name__icontains=search) | Q(contact_name__icontains=search)
        )
    query = request.GET.copy()
    query.pop("page", None)
    return render(
        request,
        "crm/migration_review.html",
        {
            "page_obj": Paginator(candidates, 25).get_page(request.GET.get("page")),
            "search": search,
            "query_string": query.urlencode(),
        },
    )


@login_required
@require_http_methods(["GET", "POST"])
def migrate_candidate(request, pk):
    if not is_system_admin(request.user):
        raise PermissionDenied
    existing = Escalation.objects.filter(prospect_id=pk).first()
    if existing:
        return redirect(existing)
    prospect = get_object_or_404(crm.review_candidates(), pk=pk)
    form = HandoffForm(
        prospect=prospect,
        historical=True,
        initial={
            "context": prospect.interest_signal
            or prospect.founder_escalation_notes
            or prospect.comments
        },
    )
    if request.method == "POST":
        form, lead = crm.escalate_prospect(
            request.user, pk, request.POST, historical=True
        )
        if lead:
            messages.success(
                request,
                "Historical lead added to the executive queue. Original outreach history is preserved.",
            )
            return redirect(lead)
    return render(
        request,
        "crm/form.html",
        {
            "form": form,
            "title": f"Review {prospect.company_name}",
            "intro": "Confirm that this historical lead belongs in the executive queue. Leave the original escalation date blank if unknown.",
            "submit_label": "Approve historical handoff",
            "back_url": "crm_migration_review",
            "prospect": prospect,
            "executive_required": True,
        },
    )
