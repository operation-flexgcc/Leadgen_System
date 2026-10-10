from datetime import timezone as datetime_timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django import forms
from django.contrib.auth import get_user_model
from django.db.models import Q
from django.utils import timezone

from .forms import DateInput, DateTimeLocalInput
from .models import Escalation, FounderCAEAssignment, Opportunity, Profile, Prospect
from .permissions import active_role, is_manager, role_membership_query


def executives():
    query = role_membership_query([Profile.Role.ACCOUNT_EXECUTIVE]) & Q(is_active=True)
    return get_user_model().objects.filter(query).distinct().order_by("first_name", "email")


class ExecutiveChoice(forms.ModelChoiceField):
    def label_from_instance(self, obj):
        return f"{obj.get_full_name()} ({obj.email})" if obj.get_full_name() else obj.email


class StyledForm:
    def style(self):
        for field in self.fields.values():
            field.widget.attrs.setdefault("class", "form-control")


class MeetingWallTimeField(forms.DateTimeField):
    def to_python(self, value):
        # The paired IANA field, not the server's default zone, interprets wall time.
        # UTC parsing avoids unrelated DST validation in the application timezone.
        with timezone.override(datetime_timezone.utc):
            return super().to_python(value)


class LegacyRoutingForm(StyledForm, forms.Form):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        current = dict(FounderCAEAssignment.objects.values_list("founder_account", "executive_id"))
        for key, label in Prospect.FounderAccount.choices:
            self.fields[key] = ExecutiveChoice(
                queryset=executives(), required=False, label=label,
                initial=current.get(key), empty_label="Leave unchanged / not configured",
                help_text="Select the existing active CAE account for this founder. Manager-only accounts are excluded.",
            )
        self.style()

    def save(self, actor):
        from django.db import transaction

        with transaction.atomic():
            for key, executive in self.cleaned_data.items():
                if executive:
                    route = FounderCAEAssignment.objects.filter(founder_account=key).first()
                    route = route or FounderCAEAssignment(founder_account=key)
                    route.executive = executive
                    route.updated_by = actor
                    route.full_clean()
                    route.save()


class SubmissionForm(StyledForm, forms.ModelForm):
    assigned_to = ExecutiveChoice(
        queryset=get_user_model().objects.none(), label="Account executive"
    )
    context = forms.CharField(
        max_length=5000,
        label="Lead context and reason for handoff",
        widget=forms.Textarea(attrs={"rows": 4}),
    )

    class Meta:
        model = Prospect
        labels = {
            "contact_linkedin_url": "LinkedIn profile",
            "workstream": "Source channel",
        }
        help_texts = {
            "contact_email": "Provide at least one contact method: email, phone or LinkedIn."
        }
        fields = [
            "workstream",
            "company_name",
            "website",
            "location",
            "contact_name",
            "contact_title",
            "contact_email",
            "contact_phone",
            "contact_linkedin_url",
        ]

    def __init__(self, *args, user, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user
        self.fields["assigned_to"].queryset = executives()
        self.fields["contact_name"].required = True
        self.fields["workstream"].choices = [
            (v, l)
            for v, l in Prospect.Workstream.choices
            if v != Prospect.Workstream.LINKEDIN_OUTREACH
        ]
        if not is_manager(user):
            self.fields["workstream"].disabled = True
            self.initial["workstream"] = active_role(user)
            self.fields["workstream"].widget = forms.HiddenInput()
        self.style()

    def clean(self):
        data = super().clean()
        if not any(
            data.get(f)
            for f in ["contact_email", "contact_phone", "contact_linkedin_url"]
        ):
            self.add_error(
                "contact_email", "Add an email, phone number, or LinkedIn profile."
            )
        return data


class HandoffForm(StyledForm, forms.Form):
    assigned_to = ExecutiveChoice(
        queryset=get_user_model().objects.none(), label="Account executive"
    )
    context = forms.CharField(
        max_length=5000,
        label="Client reply and handoff context",
        widget=forms.Textarea(attrs={"rows": 5}),
    )
    response_confirmed = forms.BooleanField(
        label="A client reply has been received and is ready for executive follow-up."
    )
    escalated_at = forms.DateTimeField(
        required=False,
        widget=DateTimeLocalInput(),
        label="Original escalation date (if known)",
        help_text="Leave blank when the historical date is unknown. Times use the application time zone.",
    )

    def __init__(self, *args, prospect, historical=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["assigned_to"].queryset = executives()
        if prospect.workstream != Prospect.Workstream.LINKEDIN_OUTREACH or historical:
            self.fields.pop("response_confirmed")
        if not historical:
            self.fields.pop("escalated_at")
        self.style()

    def clean_escalated_at(self):
        value = self.cleaned_data.get("escalated_at")
        if value and value > timezone.now():
            raise forms.ValidationError(
                "The original escalation date cannot be in the future."
            )
        return value


class LeadUpdateForm(StyledForm, forms.ModelForm):
    discovery_at = MeetingWallTimeField(
        required=False,
        label="Discovery meeting time",
        widget=DateTimeLocalInput(format="%Y-%m-%dT%H:%M"),
        help_text="Enter the time in the meeting time zone specified below.",
    )
    version = forms.IntegerField(widget=forms.HiddenInput())
    comment = forms.CharField(
        required=False,
        max_length=5000,
        widget=forms.Textarea(attrs={"rows": 3}),
        label="Add a dated comment",
    )

    class Meta:
        model = Escalation
        fields = [
            "assigned_to",
            "status",
            "next_action",
            "next_action_date",
            "discovery_at",
            "discovery_timezone",
            "discovery_outcome",
            "drop_reason",
        ]
        widgets = {
            "next_action_date": DateInput(),
            "discovery_at": DateTimeLocalInput(format="%Y-%m-%dT%H:%M"),
            "discovery_outcome": forms.Textarea(attrs={"rows": 3}),
            "drop_reason": forms.Textarea(attrs={"rows": 2}),
        }
        labels = {
            "assigned_to": "Account executive",
            "discovery_at": "Discovery meeting time",
            "discovery_timezone": "Meeting time zone (e.g. America/Chicago)",
        }
        help_texts = {
            "discovery_at": "Enter the time in the meeting time zone specified below."
        }

    def __init__(self, *args, user, **kwargs):
        super().__init__(*args, **kwargs)
        self.initial["version"] = self.instance.version
        if self.instance.discovery_at and self.instance.discovery_timezone:
            try:
                self.initial["discovery_at"] = timezone.localtime(
                    self.instance.discovery_at,
                    ZoneInfo(self.instance.discovery_timezone),
                ).replace(tzinfo=None)
            except ZoneInfoNotFoundError:
                pass
        self.fields["status"].choices = [
            (v, l)
            for v, l in Escalation.Status.choices
            if v != Escalation.Status.CONVERTED
        ]
        if is_manager(user):
            self.fields["assigned_to"] = ExecutiveChoice(
                queryset=executives(),
                label="Account executive",
            )
        else:
            self.fields.pop("assigned_to")
        self.style()

    def clean(self):
        data = super().clean()
        tz_name = data.get("discovery_timezone", "").strip()
        if tz_name:
            try:
                meeting_tz = ZoneInfo(tz_name)
            except ZoneInfoNotFoundError:
                self.add_error(
                    "discovery_timezone",
                    "Use an IANA time zone such as America/Chicago or Asia/Kolkata.",
                )
            else:
                # datetime-local is a wall-clock time in the explicitly selected zone.
                value = data.get("discovery_at")
                if value:
                    naive = value.replace(tzinfo=None)
                    if (
                        naive.replace(tzinfo=meeting_tz, fold=0).utcoffset()
                        != naive.replace(tzinfo=meeting_tz, fold=1).utcoffset()
                    ):
                        self.add_error(
                            "discovery_at",
                            "This time is ambiguous or does not exist due to daylight saving. Choose another time.",
                        )
                    else:
                        data["discovery_at"] = naive.replace(tzinfo=meeting_tz)
        if data.get("discovery_at") and not tz_name:
            self.add_error(
                "discovery_timezone",
                "Enter the meeting time zone whenever a meeting time is set.",
            )
        return data


class OpportunityForm(StyledForm, forms.ModelForm):
    version = forms.IntegerField(required=False, widget=forms.HiddenInput())
    assigned_to = ExecutiveChoice(
        queryset=get_user_model().objects.none(), label="Account executive"
    )
    comment = forms.CharField(
        required=False,
        max_length=5000,
        label="Add a dated comment",
        widget=forms.Textarea(attrs={"rows": 3}),
    )
    decision_note = forms.CharField(
        max_length=2000,
        label="Reason for moving this lead to the sales pipeline",
        widget=forms.Textarea(attrs={"rows": 2}),
    )

    class Meta:
        model = Opportunity
        fields = [
            "name",
            "requirements",
            "stage",
            "estimated_value",
            "currency",
            "probability",
            "expected_close_date",
            "next_action",
            "next_action_date",
            "closure_reason",
        ]
        widgets = {
            "expected_close_date": DateInput(),
            "next_action_date": DateInput(),
            "requirements": forms.Textarea(attrs={"rows": 4}),
            "closure_reason": forms.Textarea(attrs={"rows": 2}),
        }
        help_texts = {
            "estimated_value": "Optional until known. Values are reported separately for each currency.",
            "probability": "0–100 percent.",
            "currency": "Three-letter code, such as USD, GBP or INR.",
        }

    def __init__(self, *args, user, lead, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk:
            self.fields.pop("decision_note")
            self.initial["version"] = self.instance.version
            self.fields["version"].required = True
        else:
            self.fields.pop("version")
            self.fields["stage"].choices = [
                (v, l)
                for v, l in Opportunity.Stage.choices
                if v not in {Opportunity.Stage.WON, Opportunity.Stage.LOST}
            ]
        if is_manager(user):
            self.fields["assigned_to"].queryset = executives()
            self.initial["assigned_to"] = lead.assigned_to_id
        else:
            self.fields.pop("assigned_to")
        self.style()

    def clean_currency(self):
        return self.cleaned_data["currency"].strip().upper()


class CommentForm(StyledForm, forms.Form):
    comment = forms.CharField(
        max_length=5000,
        label="Add a dated comment",
        widget=forms.Textarea(attrs={"rows": 3}),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.style()


class CRMFilterForm(forms.Form):
    search = forms.CharField(required=False, label="Company, contact or opportunity")
    assigned_to = ExecutiveChoice(
        queryset=get_user_model().objects.none(),
        required=False,
        label="Executive",
        empty_label="All executives",
    )
    source_channel = forms.ChoiceField(
        required=False,
        choices=[("", "All channels"), *Prospect.Workstream.choices],
        label="Source channel",
    )
    status = forms.ChoiceField(required=False)
    due = forms.ChoiceField(
        required=False,
        choices=[
            ("", "All action dates"),
            ("today", "Today"),
            ("overdue", "Overdue"),
            ("upcoming", "Upcoming"),
            ("none", "No action date"),
        ],
    )
    date_from = forms.DateField(
        required=False, widget=DateInput(), label="Escalated from"
    )
    date_to = forms.DateField(
        required=False, widget=DateInput(), label="Escalated through"
    )
    close_from = forms.DateField(
        required=False, widget=DateInput(), label="Expected close from"
    )
    close_to = forms.DateField(
        required=False, widget=DateInput(), label="Expected close through"
    )

    def __init__(self, *args, pipeline=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["assigned_to"].queryset = (
            get_user_model()
            .objects.filter(role_membership_query([Profile.Role.ACCOUNT_EXECUTIVE])).distinct()
            .order_by("first_name", "email")
        )
        self.fields["status"].choices = [
            ("", "All stages" if pipeline else "All statuses"),
            *(Opportunity.Stage.choices if pipeline else Escalation.Status.choices),
        ]
        if not pipeline:
            self.fields.pop("close_from")
            self.fields.pop("close_to")
        for field in self.fields.values():
            field.widget.attrs["class"] = "filter-control"

    def clean(self):
        data = super().clean()
        for low, high in [("date_from", "date_to"), ("close_from", "close_to")]:
            if data.get(low) and data.get(high) and data[low] > data[high]:
                self.add_error(high, "The end date must be on or after the start date.")
        return data
