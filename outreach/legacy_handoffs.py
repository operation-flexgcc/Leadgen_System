"""Bridge explicit legacy escalations to the shared, CAE-only executive queue."""

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .models import CRMActivity, Escalation, FounderCAEAssignment, Outreach, Prospect
from .permissions import is_system_admin


ESCALATION_ACTIVITIES = (
    Outreach.ActivityType.FOUNDER_ESCALATION,
    Outreach.ActivityType.MEETING_HANDOFF,
)


def candidates():
    # A reply, interest signal, stage or founder selection alone is NOT a handoff.
    return Prospect.objects.filter(escalation__isnull=True).filter(
        Q(founder_escalation_required=True)
        | Q(outreaches__activity_type__in=ESCALATION_ACTIVITIES)
    ).distinct().order_by("pk")


def recipient(prospect, *, lock=False):
    routes = FounderCAEAssignment.objects
    if lock:
        routes = routes.select_for_update(of=("self",))
    route = routes.filter(
        founder_account=prospect.founder_account
    ).select_related("executive__profile").first()
    if route is None:
        raise ValidationError(
            "No CAE is mapped to this founder account. Ask a system admin to configure "
            "Legacy escalation routing, or use Escalate to executive to select an active CAE."
        )
    route.full_clean()
    return route.executive


def validate_legacy_handoff(prospect):
    existing = Escalation.objects.filter(prospect_id=prospect.pk).first() if prospect.pk else None
    if existing:
        existing.full_clean()
        return  # Never overwrite a reviewed assignment, status or opportunity.
    recipient(prospect)
    if prospect.is_not_eligible:
        raise ValidationError("Correct this prospect's not-eligible status before escalating it.")
    if not prospect.contact_name or not any([
        prospect.contact_email, prospect.contact_phone, prospect.contact_linkedin_url
    ]):
        raise ValidationError("Add a contact name and contact method before escalating it.")


def historical_evidence(prospect):
    outreach = prospect.outreaches.filter(
        activity_type__in=ESCALATION_ACTIVITIES
    ).order_by("outreach_date", "pk").first()
    audit = prospect.update_audits.filter(
        changed_values__founder_escalation_required=True,
        previous_values__founder_escalation_required=False,
    ).order_by("created_at", "pk").first()
    # Date-only outreach evidence must not be converted to an invented time.
    if outreach:
        return outreach.recorded_by, None, outreach
    if audit:
        return audit.modified_by, audit.created_at, None
    return None, None, None


@transaction.atomic
def ensure_handoff(prospect, actor, *, historical=False, outreach=None):
    # All entry points lock the same parent; OneToOne is the final duplicate guard.
    prospect = Prospect.objects.select_for_update().get(pk=prospect.pk)
    existing = Escalation.objects.filter(prospect=prospect).first()
    if existing:
        existing.full_clean()
        return existing, False
    if not (prospect.founder_escalation_required or (
        outreach and outreach.activity_type in ESCALATION_ACTIVITIES
    ) or (historical and prospect.outreaches.filter(
        activity_type__in=ESCALATION_ACTIVITIES
    ).exists())):
        return None, False
    validate_legacy_handoff(prospect)
    executive = recipient(prospect, lock=True)
    escalated_by, escalated_at = actor, timezone.now()
    evidence = outreach
    if historical:
        escalated_by, escalated_at, evidence = historical_evidence(prospect)
    parts = ["Historical legacy escalation reviewed." if historical else "Legacy escalation sent to CAE."]
    if evidence:
        parts.append(f"Outreach #{evidence.sequence_number}: {evidence.get_activity_type_display()} on {evidence.outreach_date} (date only).")
    for label, value in [
        ("Escalation notes", prospect.founder_escalation_notes),
        ("Recorded reply", evidence.response if evidence else ""),
        ("Interest", prospect.interest_signal),
        ("Questions", prospect.questions_for_founders),
    ]:
        if value:
            parts.append(f"{label}: {value}")
    context = "\n\n".join(parts)
    if len(context) > 5000:
        context = context[:4880] + "\n[Excerpt; complete notes remain on the original prospect and outreach history.]"
    lead = Escalation(
        prospect=prospect, assigned_to=executive, escalated_by=escalated_by,
        source_channel=prospect.workstream, historical=historical,
        escalated_at=escalated_at, context=context,
        next_action=prospect.next_action, next_action_date=prospect.next_action_date,
    )
    lead.full_clean()
    lead.save()
    CRMActivity.objects.create(
        escalation=lead, actor=actor,
        kind="legacy_backfill" if historical else "legacy_escalated",
        comment=context,
        changes={"assigned_to_id": {"before": None, "after": executive.pk}},
    )
    return lead, True


def preview():
    rows = []
    for prospect in candidates().select_related("owner"):
        reason = ""
        executive = None
        try:
            validate_legacy_handoff(prospect)
            executive = recipient(prospect)
            if bool(prospect.next_action.strip()) != bool(prospect.next_action_date):
                raise ValidationError("Set the original next action and its date together, or review this handoff individually.")
        except ValidationError as error:
            reason = "; ".join(error.messages)
        rows.append({"prospect": prospect, "executive": executive, "reason": reason})
    return rows


def backfill(actor, targets):
    if not is_system_admin(actor):
        raise PermissionDenied("Only a system admin can backfill historical escalations.")
    created, skipped = 0, 0
    for pk, executive_id in dict(targets).items():
        try:
            with transaction.atomic():
                prospect = Prospect.objects.select_for_update().get(pk=pk)
                # Recheck source evidence and current route; never trust a stale preview.
                if not candidates().filter(pk=pk).exists():
                    skipped += 1
                    continue
                if recipient(prospect, lock=True).pk != executive_id:
                    skipped += 1
                    continue
                _, added = ensure_handoff(prospect, actor, historical=True)
                created += int(added)
                skipped += int(not added)
        except (ValidationError, Prospect.DoesNotExist):
            skipped += 1
    return created, skipped
