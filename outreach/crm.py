"""Shared, transactional CRM operations used by HTML and bearer-token APIs."""

from django.core.exceptions import PermissionDenied, ValidationError
from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Q
from django.shortcuts import get_object_or_404
from django.utils import timezone

from .crm_forms import (
    CommentForm,
    DeadLeadForm,
    HandoffForm,
    LeadUpdateForm,
    OpportunityForm,
    SubmissionForm,
)
from .models import (
    CompanyIdentity,
    CRMActivity,
    Escalation,
    Opportunity,
    Outreach,
    Prospect,
    normalized_company_key,
)
from .permissions import (
    active_role,
    can_edit_lead,
    can_submit_lead,
    can_view_crm,
    is_manager,
    is_submission_user,
    is_system_admin,
)


def visible_leads(user):
    rows = Escalation.objects.select_related("prospect", "assigned_to", "escalated_by")
    if can_view_crm(user):
        return rows
    return rows.filter(source_channel=active_role(user)).filter(Q(prospect__owner=user) | Q(escalated_by=user))


def review_candidates():
    return (
        Prospect.objects.filter(escalation__isnull=True)
        .filter(
            ~Q(workstream=Prospect.Workstream.LINKEDIN_OUTREACH)
            | Q(founder_escalation_required=True)
            | Q(outreaches__activity_type__in=[Outreach.ActivityType.FOUNDER_ESCALATION, Outreach.ActivityType.MEETING_HANDOFF])
            | Q(
                stage__in=[
                    Prospect.Stage.RESPONDED,
                    Prospect.Stage.INTERESTED,
                    Prospect.Stage.FOUNDER_MEETING_BOOKED,
                    Prospect.Stage.MEETING_COMPLETED,
                ]
            )
            | ~Q(interest_signal="")
        )
        .select_related("owner")
        .distinct()
        .order_by("company_name", "pk")
    )


def snapshot(record, fields):
    return {
        field: (
            str(getattr(record, field)) if getattr(record, field) is not None else None
        )
        for field in fields
    }


def activity(lead, user, kind, comment="", before=None, after=None):
    changes = {
        key: {"before": value, "after": after[key]}
        for key, value in (before or {}).items()
        if value != after[key]
    }
    CRMActivity.objects.create(
        escalation=lead, actor=user, kind=kind, comment=comment, changes=changes
    )


def require_editor(user, lead):
    if not can_edit_lead(user, lead):
        raise PermissionDenied(
            "Only the assigned executive or a manager can update this record."
        )


def version_matches(form, record):
    if form.cleaned_data.get("version") != record.version:
        form.add_error(
            None,
            "This record changed while you were editing. Reload it before saving again.",
        )
        return False
    return True


def create_handoff(prospect, user, data, historical=False):
    escalated_by = user
    if historical:
        from .legacy_handoffs import historical_evidence

        # The reviewing admin is the activity actor, not the original escalator.
        escalated_by, _, _ = historical_evidence(prospect)
    lead = Escalation(
        prospect=prospect,
        assigned_to=data["assigned_to"],
        escalated_by=escalated_by,
        source_channel=prospect.workstream,
        context=data["context"],
        historical=historical,
        escalated_at=data.get("escalated_at") if historical else timezone.now(),
        next_action=prospect.next_action,
        next_action_date=prospect.next_action_date,
        next_action_owner=prospect.next_action_owner,
        approval_status=Escalation.Approval.APPROVED if historical or is_manager(user) else Escalation.Approval.PENDING,
        approved_by=user if not historical and is_manager(user) else None,
        approved_at=timezone.now() if not historical and is_manager(user) else None,
    )
    lead.full_clean()
    lead.save()
    activity(
        lead, user, "historical_handoff" if historical else "escalated", data["context"]
    )
    return lead


@transaction.atomic
def submit_lead(user, data):
    if not can_submit_lead(user):
        raise PermissionDenied("This role cannot add leads.")
    form = SubmissionForm(data, user=user)
    if not form.is_valid():
        return form, None
    prospect = form.save(commit=False)
    prospect.owner = user if active_role(user) in Prospect.Workstream.values else None
    prospect.created_by = user
    prospect.is_test = form.cleaned_data["is_test"]
    prospect.short_description = form.cleaned_data["context"][:1000]
    key = normalized_company_key(prospect.website)
    if key:
        company, _ = CompanyIdentity.objects.get_or_create(key=key)
        # Serialize same-company submissions, including concurrent duplicate requests.
        CompanyIdentity.objects.select_for_update().get(pk=company.pk)
        duplicates = Prospect.objects.filter(
            company=company, workstream=prospect.workstream
        )
    else:
        # Lock submitter to serialize no-website double submissions too.
        get_user_model().objects.select_for_update().get(pk=user.pk)
        duplicates = Prospect.objects.filter(
            company_name__iexact=prospect.company_name, workstream=prospect.workstream
        )
    if duplicates.exists():
        form.add_error(
            None,
            "This company already has a record in this channel. Open its existing record from Outreach history and escalate it, or ask a manager to assign it to you.",
        )
        return form, None
    prospect.full_clean()
    prospect.save()
    return form, create_handoff(prospect, user, form.cleaned_data)


@transaction.atomic
def approve_lead(user, lead_id, data):
    if not is_manager(user):
        raise PermissionDenied("A manager must approve this lead.")
    lead = get_object_or_404(Escalation.objects.select_for_update(), pk=lead_id)
    form = DeadLeadForm(data)
    if not form.is_valid() or not version_matches(form, lead):
        return form, None
    if lead.approval_status == Escalation.Approval.APPROVED:
        return form, lead
    lead.approval_status = Escalation.Approval.APPROVED
    lead.approved_by = user
    lead.approved_at = timezone.now()
    lead.version += 1
    lead.save(update_fields=["approval_status", "approved_by", "approved_at", "version", "updated_at"])
    activity(lead, user, "approved", "Manager approved the lead for CAE follow-up.")
    return form, lead


@transaction.atomic
def mark_dead(user, lead_id, data):
    lead = get_object_or_404(Escalation.objects.select_for_update(), pk=lead_id)
    require_editor(user, lead)
    if lead.status == Escalation.Status.CONVERTED:
        raise PermissionDenied("Close the linked sales opportunity instead.")
    form = DeadLeadForm(data)
    if not form.is_valid() or not version_matches(form, lead):
        return form, None
    before = snapshot(lead, ["status", "drop_reason", "next_action", "next_action_date", "next_action_owner_id"])
    lead.status = Escalation.Status.DROPPED
    lead.drop_reason = dict(form.fields["reason"].choices).get(form.cleaned_data["reason"], "") if form.cleaned_data["reason"] else ""
    lead.next_action = ""
    lead.next_action_date = None
    lead.next_action_owner = None
    lead.version += 1
    lead.save()
    activity(lead, user, "marked_dead", lead.drop_reason, before, snapshot(lead, before))
    return form, lead


@transaction.atomic
def remove_test_lead(user, lead_id, confirmation):
    """Deliberately limited cleanup: never delete an ordinary customer record."""
    if not is_system_admin(user):
        raise PermissionDenied("Only system administrators can remove synthetic samples.")
    lead = get_object_or_404(Escalation.objects.select_for_update(), pk=lead_id)
    prospect = Prospect.objects.select_for_update().get(pk=lead.prospect_id)
    if not prospect.is_test or confirmation != prospect.company_name:
        raise PermissionDenied("Only a labelled synthetic record with an exact name confirmation may be removed.")
    # Test samples must not be intermingled with real outreach/imports.
    if prospect.outreaches.exists() or prospect.import_batch_id:
        raise PermissionDenied("This sample has outreach or import history; ask an administrator to review it.")
    company = prospect.company
    lead.activities.all().delete()
    Opportunity.objects.filter(escalation=lead).delete()
    lead.delete()
    prospect.delete()
    if not company.prospects.exists():
        company.delete()


@transaction.atomic
def escalate_prospect(user, prospect_id, data, historical=False):
    prospect = get_object_or_404(Prospect.objects.select_for_update(), pk=prospect_id)
    if historical:
        if not is_system_admin(user):
            raise PermissionDenied(
                "Only a system administrator can migrate historical leads."
            )
    elif not (is_manager(user) or (prospect.owner_id == user.pk and prospect.workstream == active_role(user))):
        raise PermissionDenied("Claim this prospect before escalating it.")
    existing = Escalation.objects.filter(prospect=prospect).first()
    form = HandoffForm(data, prospect=prospect, historical=historical)
    if existing:
        return form, existing
    if not form.is_valid():
        return form, None
    if prospect.is_not_eligible:
        form.add_error(
            None,
            "This prospect is marked not eligible. Correct its qualification before escalating it.",
        )
        return form, None
    if not prospect.contact_name or not any(
        [prospect.contact_email, prospect.contact_phone, prospect.contact_linkedin_url]
    ):
        form.add_error(
            None,
            "Add a contact name and at least one contact method to the original prospect first.",
        )
        return form, None
    try:
        lead = create_handoff(prospect, user, form.cleaned_data, historical=historical)
    except ValidationError as error:
        form.add_error(
            None,
            "Correct the original prospect before handoff: "
            + "; ".join(error.messages),
        )
        return form, None
    return form, lead


@transaction.atomic
def update_lead(user, lead_id, data):
    lead = get_object_or_404(Escalation.objects.select_for_update(), pk=lead_id)
    require_editor(user, lead)
    if lead.status == Escalation.Status.CONVERTED:
        raise PermissionDenied("Update the linked sales opportunity after conversion.")
    before = snapshot(
        lead,
        [
            "assigned_to_id",
            *[f for f in LeadUpdateForm.Meta.fields if f != "assigned_to"],
        ],
    )
    form = LeadUpdateForm(data, instance=lead, user=user)
    if not form.is_valid() or not version_matches(form, lead):
        return form, None
    lead = form.save(commit=False)
    lead.version += 1
    lead.save()
    activity(
        lead,
        user,
        "lead_updated",
        form.cleaned_data["comment"],
        before,
        snapshot(lead, before),
    )
    return form, lead


@transaction.atomic
def convert_lead(user, lead_id, data):
    lead = get_object_or_404(Escalation.objects.select_for_update(), pk=lead_id)
    require_editor(user, lead)
    if lead.approval_status != Escalation.Approval.APPROVED:
        raise PermissionDenied("Approve this lead before moving it to the sales pipeline.")
    existing = Opportunity.objects.filter(escalation=lead).first()
    form = OpportunityForm(data, user=user, lead=lead)
    if existing:
        return form, existing
    if not form.is_valid():
        return form, None
    if lead.status == Escalation.Status.DROPPED:
        form.add_error(
            None, "Reopen the dropped lead before moving it to the sales pipeline."
        )
        return form, None
    opportunity = form.save(commit=False)
    opportunity.escalation = lead
    opportunity.save()
    before = snapshot(lead, ["status", "assigned_to_id"])
    lead.status = Escalation.Status.CONVERTED
    if "assigned_to" in form.cleaned_data:
        lead.assigned_to = form.cleaned_data["assigned_to"]
    lead.version += 1
    lead.save(update_fields=["status", "assigned_to", "version", "updated_at"])
    activity(
        lead,
        user,
        "converted",
        form.cleaned_data["decision_note"],
        before,
        snapshot(lead, before),
    )
    if form.cleaned_data["comment"]:
        activity(lead, user, "comment", form.cleaned_data["comment"])
    return form, opportunity


@transaction.atomic
def update_opportunity(user, opportunity_id, data):
    # Lock the handoff first everywhere, avoiding lock-order inversions with conversion.
    lead_id = get_object_or_404(Opportunity, pk=opportunity_id).escalation_id
    lead = Escalation.objects.select_for_update().get(pk=lead_id)
    require_editor(user, lead)
    record = Opportunity.objects.select_for_update().get(pk=opportunity_id)
    before = snapshot(record, OpportunityForm.Meta.fields)
    before["assigned_to_id"] = str(lead.assigned_to_id)
    form = OpportunityForm(data, user=user, lead=lead, instance=record)
    if not form.is_valid() or not version_matches(form, record):
        return form, None
    record = form.save(commit=False)
    if record.stage in {Opportunity.Stage.WON, Opportunity.Stage.LOST}:
        record.closed_at = record.closed_at or timezone.now()
        record.probability = 100 if record.stage == Opportunity.Stage.WON else 0
    else:
        record.closed_at = None
    record.version += 1
    record.save()
    if (
        "assigned_to" in form.cleaned_data
        and lead.assigned_to != form.cleaned_data["assigned_to"]
    ):
        lead.assigned_to = form.cleaned_data["assigned_to"]
        lead.version += 1
        lead.save(update_fields=["assigned_to", "version", "updated_at"])
    after = snapshot(record, OpportunityForm.Meta.fields)
    after["assigned_to_id"] = str(lead.assigned_to_id)
    activity(
        lead, user, "opportunity_updated", form.cleaned_data["comment"], before, after
    )
    return form, record


@transaction.atomic
def add_comment(user, lead_id, data):
    lead = get_object_or_404(Escalation.objects.select_for_update(), pk=lead_id)
    require_editor(user, lead)
    form = CommentForm(data)
    if not form.is_valid():
        return form, None
    activity(lead, user, "comment", form.cleaned_data["comment"])
    return form, lead
