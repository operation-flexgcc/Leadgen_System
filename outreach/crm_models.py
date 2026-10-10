"""Executive handoffs are separate from the original outreach ownership/history."""

from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator, RegexValidator
from django.db import models
from django.urls import reverse


class Escalation(models.Model):
    class Status(models.TextChoices):
        NEW = "new", "New"
        FOLLOWING_UP = "following_up", "Following up"
        DISCOVERY_SCHEDULED = "discovery_scheduled", "Discovery scheduled"
        DISCOVERY_COMPLETED = "discovery_completed", "Discovery completed"
        DROPPED = "dropped", "Dropped"
        CONVERTED = "converted", "Moved to sales pipeline"

    prospect = models.OneToOneField(
        "outreach.Prospect", on_delete=models.PROTECT, related_name="escalation"
    )
    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="assigned_escalations",
    )
    escalated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="submitted_escalations",
    )
    source_channel = models.CharField(max_length=30, editable=False)
    # Unknown dates on reviewed historical records must not become invented dates.
    escalated_at = models.DateTimeField(null=True, blank=True)
    historical = models.BooleanField(default=False, editable=False)
    context = models.TextField(max_length=5000)
    status = models.CharField(
        max_length=30, choices=Status.choices, default=Status.NEW, db_index=True
    )
    next_action = models.CharField(max_length=250, blank=True)
    next_action_date = models.DateField(null=True, blank=True, db_index=True)
    discovery_at = models.DateTimeField(null=True, blank=True)
    discovery_timezone = models.CharField(max_length=80, blank=True)
    discovery_outcome = models.TextField(max_length=3000, blank=True)
    drop_reason = models.TextField(max_length=2000, blank=True)
    version = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at", "-pk"]
        indexes = [models.Index(fields=["assigned_to", "status"])]

    def clean(self):
        from .models import Profile

        errors = {}
        if (
            self.assigned_to_id
            and not self.assigned_to.profile.has_role(Profile.Role.ACCOUNT_EXECUTIVE)
        ):
            errors["assigned_to"] = "Choose a client account executive."
        if bool(self.next_action.strip()) != bool(self.next_action_date):
            errors["next_action_date"] = "Set the next action and its date together."
        if self.status == self.Status.DISCOVERY_SCHEDULED:
            if not self.discovery_at:
                errors["discovery_at"] = "Enter the discovery meeting date and time."
            if not self.discovery_timezone.strip():
                errors["discovery_timezone"] = "Enter the meeting time zone."
        if (
            self.status == self.Status.DISCOVERY_COMPLETED
            and not self.discovery_outcome.strip()
        ):
            errors["discovery_outcome"] = "Record the discovery outcome."
        if self.status == self.Status.DROPPED and not self.drop_reason.strip():
            errors["drop_reason"] = "Record why the lead was dropped."
        if errors:
            raise ValidationError(errors)

    def get_absolute_url(self):
        return reverse("crm_lead_detail", args=[self.pk])

    def get_source_channel_display(self):
        from .models import Prospect

        return dict(Prospect.Workstream.choices).get(
            self.source_channel, self.source_channel
        )

    def __str__(self):
        return f"Lead {self.pk}: {self.prospect.company_name}"


class Opportunity(models.Model):
    class Stage(models.TextChoices):
        QUALIFIED = "qualified", "Qualified"
        SOLUTION = "solution", "Solution discussion"
        PROPOSAL = "proposal", "Proposal"
        NEGOTIATION = "negotiation", "Negotiation"
        WON = "won", "Won"
        LOST = "lost", "Lost"

    escalation = models.OneToOneField(
        Escalation, on_delete=models.PROTECT, related_name="opportunity"
    )
    name = models.CharField(max_length=200)
    requirements = models.TextField(max_length=5000)
    stage = models.CharField(
        max_length=20, choices=Stage.choices, default=Stage.QUALIFIED, db_index=True
    )
    estimated_value = models.DecimalField(
        max_digits=15,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(Decimal("0"))],
    )
    currency = models.CharField(
        max_length=3,
        blank=True,
        validators=[
            RegexValidator(
                r"^[A-Z]{3}$",
                "Use a three-letter currency code, for example USD or INR.",
            )
        ],
    )
    probability = models.PositiveSmallIntegerField(
        default=0, validators=[MaxValueValidator(100)]
    )
    expected_close_date = models.DateField(null=True, blank=True, db_index=True)
    next_action = models.CharField(max_length=250, blank=True)
    next_action_date = models.DateField(null=True, blank=True, db_index=True)
    closure_reason = models.TextField(max_length=2000, blank=True)
    closed_at = models.DateTimeField(null=True, blank=True, editable=False)
    version = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at", "-pk"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(probability__lte=100),
                name="crm_probability_at_most_100",
            ),
            models.CheckConstraint(
                condition=models.Q(estimated_value__gte=0)
                | models.Q(estimated_value__isnull=True),
                name="crm_nonnegative_deal_value",
            ),
        ]

    def clean(self):
        errors = {}
        if self.estimated_value is not None and not self.currency:
            errors["currency"] = "Choose the currency for this value."
        if bool(self.next_action.strip()) != bool(self.next_action_date):
            errors["next_action_date"] = "Set the next action and its date together."
        if (
            self.stage in {self.Stage.WON, self.Stage.LOST}
            and not self.closure_reason.strip()
        ):
            errors["closure_reason"] = "Record the reason for closing this opportunity."
        if errors:
            raise ValidationError(errors)

    def get_absolute_url(self):
        return reverse("crm_opportunity_detail", args=[self.pk])

    def __str__(self):
        return self.name


class CRMActivity(models.Model):
    escalation = models.ForeignKey(
        Escalation, on_delete=models.PROTECT, related_name="activities"
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="crm_activities",
    )
    kind = models.CharField(max_length=30)
    comment = models.TextField(max_length=5000, blank=True)
    changes = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-pk"]
