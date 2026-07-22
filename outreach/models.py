import uuid
from urllib.parse import urlsplit

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.urls import reverse


class Profile(models.Model):
    class Role(models.TextChoices):
        INTERN = "intern", "Sales intern"
        INSIDE_SALES = "inside_sales", "Inside sales"
        LINKEDIN_OUTREACH = "linkedin_outreach", "LinkedIn outreach"
        MANAGER = "manager", "Manager"
        SYSTEM_ADMIN = "system_admin", "System admin"

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="profile")
    role = models.CharField(max_length=20, choices=Role.choices, default=Role.INTERN, db_index=True)

    def __str__(self):
        return f"{self.user.get_full_name() or self.user.email or self.user.username} ({self.get_role_display()})"


def normalized_company_key(website):
    host = (urlsplit(website or "").hostname or "").lower().removeprefix("www.")
    return host


class CompanyIdentity(models.Model):
    """Stable identity shared by every workstream record for one company."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    key = models.CharField(max_length=300, unique=True, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["key"]
        verbose_name_plural = "company identities"

    def __str__(self):
        return str(self.id)


class Prospect(models.Model):
    class Workstream(models.TextChoices):
        INTERN = Profile.Role.INTERN, "Sales intern"
        INSIDE_SALES = Profile.Role.INSIDE_SALES, "Inside sales"
        LINKEDIN_OUTREACH = Profile.Role.LINKEDIN_OUTREACH, "LinkedIn outreach"

    class Stage(models.TextChoices):
        RESEARCH = "research", "Research required"
        ELIGIBLE = "eligible", "Eligible"
        CONTACTED = "contacted", "Contacted"
        RESPONDED = "responded", "Responded"
        INTERESTED = "interested", "Interested"
        FOUNDER_MEETING_BOOKED = "founder_meeting_booked", "Founder meeting booked"
        MEETING_COMPLETED = "meeting_completed", "Meeting completed"
        CLOSED = "closed", "Closed"

    class Status(models.TextChoices):
        NOT_RESPONDED = "not_yet_responded", "Not yet responded"
        NOT_INTERESTED = "not_interested", "Not interested"
        MEETING_TO_SCHEDULE = "meeting_to_be_scheduled", "Meeting to be scheduled"
        MEETING_SCHEDULED = "meeting_scheduled", "Meeting scheduled"
        MEETING_DONE = "meeting_done", "Meeting done"

    class MaterialShared(models.TextChoices):
        NONE = "", "Nothing shared"
        ONE_PAGE = "one_page", "One-page overview"
        SHORT_DECK = "short_deck", "Short introductory deck"
        ONE_PAGE_AND_DECK = "one_page_and_deck", "One-page overview and short deck"

    class FounderAccount(models.TextChoices):
        KANDARP_SONI = "kandarp_soni", "Kandarp Soni (Chicago)"
        SUNIT_KALA = "sunit_kala", "Sunit Kala (Florida)"

    class LinkedInConnectionStatus(models.TextChoices):
        NOT_SENT = "not_sent", "Connection request not sent"
        REQUEST_SENT = "request_sent", "Connection request sent"
        ACCEPTED = "accepted", "Connection accepted"
        DECLINED = "declined", "Declined or not now"

    company = models.ForeignKey(
        CompanyIdentity,
        blank=True,
        on_delete=models.PROTECT,
        related_name="prospects",
        editable=False,
        help_text="System-generated immutable company identity shared across workstreams.",
    )
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="prospects",
        help_text="Outreach operator responsible for this prospect.",
        null=True,
        blank=True,
    )
    workstream = models.CharField(
        max_length=30,
        choices=Workstream.choices,
        default=Workstream.INTERN,
        db_index=True,
    )
    stage = models.CharField(
        max_length=40,
        choices=Stage.choices,
        default=Stage.ELIGIBLE,
        db_index=True,
    )
    company_name = models.CharField(max_length=200, db_index=True)
    website = models.URLField(max_length=500)
    short_description = models.TextField(max_length=1000, blank=True)
    location = models.CharField(max_length=200, blank=True)
    consulting_focus = models.CharField(max_length=300, blank=True)
    client_segment = models.CharField(max_length=300, blank=True)
    eligibility_evidence = models.TextField(blank=True)
    contact_name = models.CharField(max_length=200, blank=True)
    contact_title = models.CharField(max_length=200, blank=True)
    contact_linkedin_url = models.URLField(max_length=500, blank=True)
    contact_email = models.EmailField(blank=True)
    contact_phone = models.CharField(max_length=50, blank=True)
    founder_account = models.CharField(max_length=30, choices=FounderAccount.choices, blank=True)
    linkedin_connection_status = models.CharField(
        max_length=30,
        choices=LinkedInConnectionStatus.choices,
        blank=True,
    )
    personalization_note = models.CharField(max_length=500, blank=True)
    founder_escalation_required = models.BooleanField(default=False)
    founder_escalation_notes = models.TextField(blank=True)
    material_shared = models.CharField(max_length=30, choices=MaterialShared.choices, blank=True)
    interest_signal = models.TextField(blank=True)
    questions_for_founders = models.TextField(blank=True)
    next_action = models.CharField(max_length=250, blank=True)
    next_action_date = models.DateField(null=True, blank=True, db_index=True)
    status = models.CharField(
        max_length=40,
        choices=Status.choices,
        default=Status.NOT_RESPONDED,
        db_index=True,
    )
    meeting_scheduled_at = models.DateTimeField(null=True, blank=True)
    meeting_timezone = models.CharField(max_length=80, blank=True)
    meeting_participants = models.CharField(max_length=500, blank=True)
    import_source = models.CharField(max_length=120, blank=True)
    import_key = models.CharField(max_length=300, blank=True)
    comments = models.TextField(blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="created_prospects",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["next_action_date", "company_name"]
        indexes = [
            models.Index(fields=["owner", "status"]),
            models.Index(fields=["owner", "next_action_date"]),
            models.Index(fields=["workstream", "stage"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["workstream", "import_key"],
                condition=~models.Q(import_key=""),
                name="unique_imported_firm_per_workstream",
            ),
        ]

    def clean(self):
        errors = {}
        research_required = self.stage == self.Stage.RESEARCH
        if not research_required and not any([self.contact_email, self.contact_phone, self.contact_linkedin_url]):
            errors["contact_email"] = "Add at least one contact method: email, phone, or LinkedIn."
        if self.owner_id:
            try:
                owner_role = self.owner.profile.role
            except Profile.DoesNotExist:
                owner_role = None
            if owner_role not in self.Workstream.values:
                errors["owner"] = "Assign this prospect to a sales intern, inside-sales user, or LinkedIn-outreach user."
            elif owner_role != self.workstream:
                errors["owner"] = "The assigned user's class must match the selected workstream."
        if not research_required and self.workstream == self.Workstream.LINKEDIN_OUTREACH:
            if not self.contact_linkedin_url:
                errors["contact_linkedin_url"] = "A LinkedIn profile is required for LinkedIn outreach."
            if not self.founder_account:
                errors["founder_account"] = "Choose the single founder account used for this prospect."
            if not self.linkedin_connection_status:
                errors["linkedin_connection_status"] = "Record the current LinkedIn connection state."
            if not self.personalization_note:
                errors["personalization_note"] = "Record the factual personalization used for the connection request."
        if self.status == self.Status.MEETING_SCHEDULED:
            if not self.meeting_scheduled_at:
                errors["meeting_scheduled_at"] = "Enter the meeting date and time when status is Meeting scheduled."
            if not self.meeting_timezone:
                errors["meeting_timezone"] = "Enter the meeting time zone."
            if not self.meeting_participants:
                errors["meeting_participants"] = "Record the confirmed meeting participants."
        if self.next_action and not self.next_action_date:
            errors["next_action_date"] = "Enter a next action date when a next action is set."
        if self.next_action_date and not self.next_action:
            errors["next_action"] = "Describe the next action for this date."
        if errors:
            raise ValidationError(errors)

    def sync_stage_from_status(self):
        status_stages = {
            self.Status.NOT_INTERESTED: self.Stage.CLOSED,
            self.Status.MEETING_TO_SCHEDULE: self.Stage.INTERESTED,
            self.Status.MEETING_SCHEDULED: self.Stage.FOUNDER_MEETING_BOOKED,
            self.Status.MEETING_DONE: self.Stage.MEETING_COMPLETED,
        }
        if self.status in status_stages:
            self.stage = status_stages[self.status]

    def get_absolute_url(self):
        return reverse("prospect_detail", kwargs={"pk": self.pk})

    def save(self, *args, **kwargs):
        company_key = normalized_company_key(self.website)
        if self._state.adding:
            if not company_key:
                raise ValidationError({"website": "Enter a valid company website before saving."})
            self.company, _ = CompanyIdentity.objects.get_or_create(key=company_key)
        else:
            original = type(self).objects.only("company_id").get(pk=self.pk)
            if self.company_id != original.company_id:
                raise ValidationError({"company": "The system-generated company ID cannot be changed."})
        return super().save(*args, **kwargs)

    def __str__(self):
        return self.company_name


class Outreach(models.Model):
    class Medium(models.TextChoices):
        PHONE = "phone", "Phone"
        EMAIL = "email", "Email"
        LINKEDIN = "linkedin", "LinkedIn"

    class ActivityType(models.TextChoices):
        CONNECTION_REQUEST = "connection_request", "LinkedIn connection request"
        CONNECTION_ACCEPTED = "connection_accepted", "LinkedIn connection accepted"
        INITIAL_OUTREACH = "initial_outreach", "Initial outreach"
        FOLLOW_UP = "follow_up", "Follow-up"
        MATERIAL_SENT = "material_sent", "One-page overview or deck sent"
        CLOSE_LOOP = "close_loop", "Close-the-loop message"
        INBOUND_RESPONSE = "inbound_response", "Prospect response"
        PHONE_CALL = "phone_call", "Phone call"
        VOICEMAIL = "voicemail", "Voicemail"
        FOUNDER_ESCALATION = "founder_escalation", "Escalated to founder"
        MEETING_HANDOFF = "meeting_handoff", "Founder meeting handoff"

    prospect = models.ForeignKey(Prospect, on_delete=models.CASCADE, related_name="outreaches")
    sequence_number = models.PositiveSmallIntegerField(
        validators=[MinValueValidator(1), MaxValueValidator(5)]
    )
    activity_type = models.CharField(
        max_length=40,
        choices=ActivityType.choices,
        default=ActivityType.INITIAL_OUTREACH,
    )
    medium = models.CharField(max_length=20, choices=Medium.choices)
    outreach_date = models.DateField()
    response = models.TextField(blank=True)
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="recorded_outreaches",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["sequence_number"]
        constraints = [
            models.UniqueConstraint(fields=["prospect", "sequence_number"], name="unique_prospect_outreach_number"),
            models.CheckConstraint(
                condition=models.Q(sequence_number__gte=1, sequence_number__lte=5),
                name="outreach_number_between_1_and_5",
            ),
        ]

    def __str__(self):
        return f"{self.prospect.company_name} outreach {self.sequence_number}"


class ApiRefreshToken(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="api_refresh_tokens",
    )
    token_hash = models.CharField(max_length=64, unique=True)
    expires_at = models.DateTimeField(db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"API refresh token for {self.user} created {self.created_at}"


class CompanyUpdateAudit(models.Model):
    class Source(models.TextChoices):
        API = "api", "API"

    company = models.ForeignKey(
        CompanyIdentity,
        on_delete=models.PROTECT,
        related_name="update_audits",
    )
    modified_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="company_update_audits",
    )
    source = models.CharField(max_length=20, choices=Source.choices, default=Source.API)
    previous_values = models.JSONField(default=dict)
    changed_values = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.company_id} updated by {self.modified_by}"
