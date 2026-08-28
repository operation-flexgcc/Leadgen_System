from .models import Outreach, Prospect


def sync_prospect_from_outreach(prospect, outreach):
    """Apply the prospect state changes implied by one outreach record."""
    update_fields = set()
    if prospect.stage == Prospect.Stage.ELIGIBLE:
        prospect.stage = Prospect.Stage.CONTACTED
        update_fields.add("stage")
    if outreach.response and prospect.stage in {
        Prospect.Stage.ELIGIBLE,
        Prospect.Stage.CONTACTED,
    }:
        prospect.stage = Prospect.Stage.RESPONDED
        update_fields.add("stage")
    if (
        outreach.activity_type == Outreach.ActivityType.CONNECTION_REQUEST
        and prospect.linkedin_connection_status
        in {"", Prospect.LinkedInConnectionStatus.NOT_SENT}
    ):
        prospect.linkedin_connection_status = Prospect.LinkedInConnectionStatus.REQUEST_SENT
        update_fields.add("linkedin_connection_status")
    elif outreach.activity_type == Outreach.ActivityType.CONNECTION_ACCEPTED:
        prospect.linkedin_connection_status = Prospect.LinkedInConnectionStatus.ACCEPTED
        update_fields.add("linkedin_connection_status")
    elif (
        outreach.activity_type == Outreach.ActivityType.MATERIAL_SENT
        or (
            prospect.workstream == Prospect.Workstream.LINKEDIN_OUTREACH
            and outreach.activity_type == Outreach.ActivityType.INITIAL_OUTREACH
        )
    ) and not prospect.material_shared:
        prospect.material_shared = Prospect.MaterialShared.ONE_PAGE
        update_fields.add("material_shared")
    elif outreach.activity_type == Outreach.ActivityType.FOUNDER_ESCALATION:
        prospect.founder_escalation_required = True
        if outreach.response:
            prospect.founder_escalation_notes = outreach.response
            update_fields.add("founder_escalation_notes")
        update_fields.add("founder_escalation_required")
    if update_fields:
        update_fields.add("updated_at")
        prospect.save(update_fields=update_fields)
