import uuid
from urllib.parse import urlsplit

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models, transaction
from django.urls import reverse
from django.utils import timezone

from .crm_models import CRMActivity, Escalation, Opportunity  # noqa: F401


class Profile(models.Model):
    class Role(models.TextChoices):
        INTERN = "intern", "Sales intern"
        INSIDE_SALES = "inside_sales", "Inside sales"
        LINKEDIN_OUTREACH = "linkedin_outreach", "LinkedIn outreach"
        ACCOUNT_EXECUTIVE = "account_executive", "Client account executive"
        MANAGER = "manager", "Manager"
        SYSTEM_ADMIN = "system_admin", "System admin"
        AI_USER = "ai_user", "AI user"

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="profile")
    role = models.CharField(max_length=20, choices=Role.choices, default=Role.INTERN, db_index=True)
    password_provisioned_by_admin = models.BooleanField(default=False, editable=False)

    def __str__(self):
        return f"{self.user.get_full_name() or self.user.email or self.user.username} ({self.get_role_display()})"

    @property
    def assigned_roles(self):
        # Keep the existing role as a primary grant for backwards compatibility.
        extra = {grant.role for grant in self.additional_roles.all()}
        return [self.role] + [role for role in self.Role.values if role in extra and role != self.role]

    @property
    def role_choices(self):
        labels = dict(self.Role.choices)
        return [(role, labels[role]) for role in self.assigned_roles]

    @property
    def roles_display(self):
        return ", ".join(label for _, label in self.role_choices)

    def has_role(self, role):
        return role in self.assigned_roles

    @transaction.atomic
    def set_roles(self, roles):
        roles = list(dict.fromkeys(roles))
        if not roles or any(role not in self.Role.values for role in roles):
            raise ValidationError("Choose at least one valid user role.")
        if self.role not in roles:
            self.role = roles[0]
        self.save(update_fields=["role"])
        self.additional_roles.exclude(role__in=roles).delete()
        self.additional_roles.filter(role=self.role).delete()
        for role in roles:
            if role != self.role:
                self.additional_roles.get_or_create(role=role)
        self.user.api_access_tokens.filter(revoked_at__isnull=True).exclude(role__in=roles).update(revoked_at=timezone.now())
        if hasattr(self, "_prefetched_objects_cache"):
            self._prefetched_objects_cache.pop("additional_roles", None)


class ProfileRole(models.Model):
    profile = models.ForeignKey(Profile, on_delete=models.CASCADE, related_name="additional_roles")
    role = models.CharField(max_length=20, choices=Profile.Role.choices, db_index=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["profile", "role"], name="unique_additional_profile_role")]


def normalized_company_key(website):
    host = (urlsplit(website or "").hostname or "").lower().removeprefix("www.")
    return host


class FounderCAEAssignment(models.Model):
    """Explicit legacy founder-account routing; never infer recipients from names."""

    founder_account = models.CharField(max_length=30, unique=True)
    executive = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="founder_cae_assignments"
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="updated_cae_assignments"
    )
    updated_at = models.DateTimeField(auto_now=True)

    def clean(self):
        if self.founder_account not in Prospect.FounderAccount.values:
            raise ValidationError({"founder_account": "Choose a recognized founder account."})
        if self.executive_id and (
            not self.executive.is_active
            or not self.executive.profile.has_role(Profile.Role.ACCOUNT_EXECUTIVE)
        ):
            raise ValidationError({"executive": "Choose an active client account executive."})


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
    class LeadSource(models.TextChoices):
        PERSONAL = "personal_contact", "Personal contact"
        LINKEDIN = "linkedin", "LinkedIn"
        INBOUND = "inbound", "Inbound"
        REFERRAL = "referral", "Referral"
        OUTBOUND = "outbound", "Outbound"
        EVENT = "event", "Event / networking"
        OTHER = "other", "Other"

    class Workstream(models.TextChoices):
        INTERN = Profile.Role.INTERN, "Sales intern"
        INSIDE_SALES = Profile.Role.INSIDE_SALES, "Inside sales"
        LINKEDIN_OUTREACH = Profile.Role.LINKEDIN_OUTREACH, "LinkedIn outreach"

    class Stage(models.TextChoices):
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
    lead_source = models.CharField(max_length=30, choices=LeadSource.choices, blank=True)
    first_contact_date = models.DateField(null=True, blank=True)
    is_test = models.BooleanField(default=False, editable=False)
    website = models.URLField(max_length=500, blank=True)
    short_description = models.TextField(max_length=1000, blank=True)
    location = models.CharField(max_length=200, blank=True)
    consulting_focus = models.CharField(max_length=300, blank=True)
    client_segment = models.CharField(max_length=300, blank=True)
    eligibility_evidence = models.TextField(blank=True)
    is_not_eligible = models.BooleanField("Is not eligible", default=False)
    contact_name = models.CharField(max_length=200, blank=True)
    contact_title = models.CharField(max_length=200, blank=True)
    contact_linkedin_url = models.URLField(max_length=500, blank=True)
    contact_email = models.EmailField(blank=True)
    contact_phone = models.CharField(max_length=50, blank=True)
    prospect_sent = models.BooleanField("Prospect sent", default=False)
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
    next_action_owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
        related_name="prospect_next_actions",
    )
    status = models.CharField(
        max_length=40,
        choices=Status.choices,
        default=Status.NOT_RESPONDED,
        db_index=True,
    )
    meeting_scheduled_at = models.DateTimeField(null=True, blank=True)
    meeting_timezone = models.CharField(max_length=80, blank=True)
    meeting_participants = models.CharField(max_length=500, blank=True)
    import_batch = models.ForeignKey(
        "ProspectImportBatch",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="prospects",
        editable=False,
    )
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
        if self.owner_id:
            try:
                owner_roles = self.owner.profile.assigned_roles
            except Profile.DoesNotExist:
                owner_roles = []
            if not set(owner_roles).intersection(self.Workstream.values):
                errors["owner"] = "Assign this prospect to a sales intern, inside-sales user, or LinkedIn-outreach user."
            elif self.workstream not in owner_roles:
                errors["owner"] = "The assigned user's class must match the selected workstream."
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
                company_key = f"lead:{uuid.uuid4()}"
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


class ApiAccessToken(models.Model):
    """Persistent API credential whose raw value is shown only when issued."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="api_access_tokens",
    )
    token_hash = models.CharField(max_length=64, unique=True)
    token_prefix = models.CharField(max_length=16)
    role = models.CharField(max_length=20, choices=Profile.Role.choices)
    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"API access token {self.token_prefix} for {self.user}"


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


class ProspectUpdateAudit(models.Model):
    class Source(models.TextChoices):
        API = "api", "API"

    prospect = models.ForeignKey(
        Prospect,
        on_delete=models.PROTECT,
        related_name="update_audits",
    )
    modified_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="prospect_update_audits",
    )
    source = models.CharField(max_length=20, choices=Source.choices, default=Source.API)
    previous_values = models.JSONField(default=dict)
    changed_values = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Prospect {self.prospect_id} updated by {self.modified_by}"


class OutreachUpdateAudit(models.Model):
    class Source(models.TextChoices):
        API = "api", "API"

    outreach = models.ForeignKey(
        Outreach,
        on_delete=models.PROTECT,
        related_name="update_audits",
    )
    modified_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="outreach_update_audits",
    )
    source = models.CharField(max_length=20, choices=Source.choices, default=Source.API)
    previous_values = models.JSONField(default=dict)
    changed_values = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Outreach {self.outreach_id} updated by {self.modified_by}"


class ProspectImportBatch(models.Model):
    class Status(models.TextChoices):
        ACTIVE = "active", "Imported"
        PARTIALLY_ROLLED_BACK = "partially_rolled_back", "Partially rolled back"
        ROLLED_BACK = "rolled_back", "Rolled back"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="prospect_import_batches",
    )
    source_filename = models.CharField(max_length=255)
    target_workstream = models.CharField(max_length=30, choices=Prospect.Workstream.choices)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="assigned_prospect_import_batches",
    )
    total_rows = models.PositiveIntegerField(default=0)
    created_count = models.PositiveIntegerField(default=0)
    skipped_count = models.PositiveIntegerField(default=0)
    skipped_rows = models.JSONField(default=list)
    created_rows = models.JSONField(default=list)
    status = models.CharField(max_length=30, choices=Status.choices, default=Status.ACTIVE)
    rolled_back_count = models.PositiveIntegerField(default=0)
    rollback_skipped_rows = models.JSONField(default=list)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    rolled_back_at = models.DateTimeField(null=True, blank=True)
    rolled_back_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="rolled_back_prospect_import_batches",
    )

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.source_filename} imported by {self.uploaded_by}"
