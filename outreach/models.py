from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.urls import reverse


class Profile(models.Model):
    class Role(models.TextChoices):
        INTERN = "intern", "Sales intern"
        MANAGER = "manager", "Manager"
        SYSTEM_ADMIN = "system_admin", "System admin"

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="profile")
    role = models.CharField(max_length=20, choices=Role.choices, default=Role.INTERN, db_index=True)

    def __str__(self):
        return f"{self.user.get_full_name() or self.user.email or self.user.username} ({self.get_role_display()})"


class Prospect(models.Model):
    class Status(models.TextChoices):
        NOT_RESPONDED = "not_yet_responded", "Not yet responded"
        NOT_INTERESTED = "not_interested", "Not interested"
        MEETING_TO_SCHEDULE = "meeting_to_be_scheduled", "Meeting to be scheduled"
        MEETING_SCHEDULED = "meeting_scheduled", "Meeting scheduled"
        MEETING_DONE = "meeting_done", "Meeting done"

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="prospects",
        help_text="Sales intern responsible for this prospect.",
    )
    company_name = models.CharField(max_length=200, db_index=True)
    website = models.URLField(max_length=500)
    short_description = models.TextField(max_length=1000)
    contact_name = models.CharField(max_length=200)
    contact_linkedin_url = models.URLField(max_length=500, blank=True)
    contact_email = models.EmailField(blank=True)
    contact_phone = models.CharField(max_length=50, blank=True)
    next_action = models.CharField(max_length=250, blank=True)
    next_action_date = models.DateField(null=True, blank=True, db_index=True)
    status = models.CharField(
        max_length=40,
        choices=Status.choices,
        default=Status.NOT_RESPONDED,
        db_index=True,
    )
    meeting_scheduled_at = models.DateTimeField(null=True, blank=True)
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
        ]

    def clean(self):
        errors = {}
        if not any([self.contact_email, self.contact_phone, self.contact_linkedin_url]):
            errors["contact_email"] = "Add at least one contact method: email, phone, or LinkedIn."
        if self.status == self.Status.MEETING_SCHEDULED and not self.meeting_scheduled_at:
            errors["meeting_scheduled_at"] = "Enter the meeting date and time when status is Meeting scheduled."
        if self.next_action and not self.next_action_date:
            errors["next_action_date"] = "Enter a next action date when a next action is set."
        if self.next_action_date and not self.next_action:
            errors["next_action"] = "Describe the next action for this date."
        if errors:
            raise ValidationError(errors)

    def get_absolute_url(self):
        return reverse("prospect_detail", kwargs={"pk": self.pk})

    def __str__(self):
        return self.company_name


class Outreach(models.Model):
    class Medium(models.TextChoices):
        PHONE = "phone", "Phone"
        EMAIL = "email", "Email"
        LINKEDIN = "linkedin", "LinkedIn"

    prospect = models.ForeignKey(Prospect, on_delete=models.CASCADE, related_name="outreaches")
    sequence_number = models.PositiveSmallIntegerField(
        validators=[MinValueValidator(1), MaxValueValidator(5)]
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
