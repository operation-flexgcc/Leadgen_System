from urllib.parse import urlsplit

from django import forms
from django.contrib.auth import get_user_model
from allauth.account.models import EmailAddress
from django.db.models import Q
from django.utils import timezone

from .models import Outreach, Profile, Prospect
from .permissions import is_manager


class DateInput(forms.DateInput):
    input_type = "date"


class DateTimeLocalInput(forms.DateTimeInput):
    input_type = "datetime-local"


class OperatorSelect(forms.Select):
    def create_option(self, name, value, label, selected, index, subindex=None, attrs=None):
        option = super().create_option(name, value, label, selected, index, subindex, attrs)
        if value and getattr(value, "instance", None):
            try:
                option["attrs"]["data-role"] = value.instance.profile.role
            except Profile.DoesNotExist:
                pass
        return option


class ProspectForm(forms.ModelForm):
    class Meta:
        model = Prospect
        fields = [
            "workstream",
            "owner",
            "company_name",
            "website",
            "short_description",
            "location",
            "consulting_focus",
            "client_segment",
            "eligibility_evidence",
            "is_not_eligible",
            "contact_name",
            "contact_title",
            "contact_linkedin_url",
            "contact_email",
            "contact_phone",
            "prospect_sent",
            "founder_account",
            "linkedin_connection_status",
            "personalization_note",
            "founder_escalation_required",
            "founder_escalation_notes",
            "material_shared",
            "interest_signal",
            "questions_for_founders",
            "next_action",
            "next_action_date",
            "status",
            "meeting_scheduled_at",
            "meeting_timezone",
            "meeting_participants",
            "comments",
        ]
        widgets = {
            "owner": OperatorSelect(),
            "short_description": forms.Textarea(attrs={"rows": 3}),
            "eligibility_evidence": forms.Textarea(attrs={"rows": 3}),
            "founder_escalation_notes": forms.Textarea(attrs={"rows": 3}),
            "interest_signal": forms.Textarea(attrs={"rows": 3}),
            "questions_for_founders": forms.Textarea(attrs={"rows": 3}),
            "next_action_date": DateInput(),
            "meeting_scheduled_at": DateTimeLocalInput(format="%Y-%m-%dT%H:%M"),
            "comments": forms.Textarea(attrs={"rows": 4}),
        }
        help_texts = {
            "short_description": "What the company does and why it may be relevant.",
            "location": "City and state, for example Chicago, Illinois.",
            "consulting_focus": "The firm's primary consulting or advisory practice.",
            "client_segment": "Evidence that it serves small, growing, or mid-sized businesses.",
            "eligibility_evidence": "Verified facts supporting boutique consultancy and SMB/mid-market fit.",
            "is_not_eligible": "Mark this explicitly when the prospect fails the eligibility check.",
            "prospect_sent": "Tracked separately from outreach activity and pipeline status.",
            "personalization_note": "One short factual reference to the person, firm, location, or practice.",
            "founder_escalation_notes": "Record the exact question or reply the founder needs to handle.",
            "interest_signal": "Exact reply or a concise factual summary of the prospect's interest.",
            "questions_for_founders": "Only questions explicitly raised by the prospect.",
            "next_action": "Example: Send product brief or call procurement lead.",
            "meeting_scheduled_at": "Required when status is Meeting scheduled.",
            "meeting_timezone": "Required for a scheduled founder meeting, for example America/Chicago or ET.",
            "meeting_participants": "Names of the prospect, founder(s), and any other confirmed attendees.",
        }

    def __init__(self, *args, user, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user
        self.fields["meeting_scheduled_at"].input_formats = ["%Y-%m-%dT%H:%M"]
        selected_workstream = (
            self.data.get("workstream")
            if self.is_bound
            else self.initial.get("workstream")
            or (self.instance.workstream if self.instance and self.instance.pk else None)
            or getattr(getattr(user, "profile", None), "role", Prospect.Workstream.INTERN)
        )
        self.workstream_value = selected_workstream

        for field_name in [
            "short_description",
            "location",
            "consulting_focus",
            "client_segment",
            "eligibility_evidence",
            "contact_name",
            "contact_title",
        ]:
            self.fields[field_name].required = True

        if selected_workstream == Prospect.Workstream.LINKEDIN_OUTREACH:
            for field_name in [
                "contact_linkedin_url",
                "founder_account",
                "linkedin_connection_status",
                "personalization_note",
            ]:
                self.fields[field_name].required = True

        if is_manager(user):
            owner_query = Q(profile__role__in=Prospect.Workstream.values, is_active=True)
            if self.instance and self.instance.pk:
                owner_query |= Q(pk=self.instance.owner_id)
            self.fields["owner"].queryset = (
                get_user_model().objects.filter(owner_query).select_related("profile").order_by("first_name", "email")
            )
            self.fields["owner"].label = "Assigned outreach user"
            self.fields["owner"].empty_label = "Unassigned role queue"
            self.fields["owner"].label_from_instance = lambda operator: (
                f"{operator.get_full_name() or operator.email} - {operator.profile.get_role_display()}"
            )
        else:
            self.fields.pop("owner")
            self.fields["workstream"].widget = forms.HiddenInput()
            self.fields["workstream"].initial = getattr(user.profile, "role", Prospect.Workstream.INTERN)

        for name, field in self.fields.items():
            field.widget.attrs.setdefault("class", "form-control")
            if field.required:
                field.widget.attrs["aria-required"] = "true"

    @staticmethod
    def _normalized_website_host(value):
        host = (urlsplit(value).hostname or "").lower()
        return host.removeprefix("www.")

    @staticmethod
    def _normalized_linkedin_url(value):
        return value.strip().lower().rstrip("/")

    def clean(self):
        cleaned_data = super().clean()
        if not is_manager(self.user):
            cleaned_data["workstream"] = self.user.profile.role
            self.instance.workstream = self.user.profile.role

        workstream = cleaned_data.get("workstream")
        owner = cleaned_data.get("owner") if is_manager(self.user) else self.user
        if owner and workstream and owner.profile.role != workstream:
            self.add_error("owner", "Choose an outreach user whose class matches the selected workstream.")

        website = cleaned_data.get("website")
        linkedin_url = cleaned_data.get("contact_linkedin_url")
        website_host = self._normalized_website_host(website) if website else ""
        normalized_linkedin = self._normalized_linkedin_url(linkedin_url) if linkedin_url else ""
        duplicates = (
            Prospect.objects.exclude(pk=self.instance.pk)
            .filter(workstream=workstream)
            .select_related("owner")
        )
        for existing in duplicates.only("company_name", "website", "contact_linkedin_url", "owner__email"):
            same_website = website_host and website_host == self._normalized_website_host(existing.website)
            same_contact = (
                normalized_linkedin
                and normalized_linkedin == self._normalized_linkedin_url(existing.contact_linkedin_url)
            )
            if same_website or same_contact:
                if existing.owner:
                    operator = existing.owner.get_full_name() or existing.owner.email
                else:
                    operator = "the unassigned role queue"
                match = "company website" if same_website else "contact LinkedIn profile"
                raise forms.ValidationError(
                    f"Potential duplicate in the {existing.get_workstream_display()} workstream: "
                    f"{existing.company_name} already uses this {match} and is assigned to {operator}. "
                    "Claim or transfer the existing record instead of creating another record in the same workstream."
                )

        return cleaned_data

    def save(self, commit=True):
        prospect = super().save(commit=False)
        if not is_manager(self.user):
            prospect.owner = self.user
            prospect.workstream = self.user.profile.role
        if prospect.stage == Prospect.Stage.RESEARCH:
            prospect.stage = Prospect.Stage.ELIGIBLE
        if prospect.workstream != Prospect.Workstream.LINKEDIN_OUTREACH:
            prospect.founder_account = ""
            prospect.linkedin_connection_status = ""
            prospect.personalization_note = ""
            prospect.founder_escalation_required = False
            prospect.founder_escalation_notes = ""
        prospect.sync_stage_from_status()
        if commit:
            prospect.save()
        return prospect


class OutreachForm(forms.ModelForm):
    STANDARD_ACTIVITY_CHOICES = [
        (Outreach.ActivityType.INITIAL_OUTREACH, "Initial outreach"),
        (Outreach.ActivityType.FOLLOW_UP, "Follow-up"),
        (Outreach.ActivityType.PHONE_CALL, "Phone call"),
        (Outreach.ActivityType.VOICEMAIL, "Voicemail"),
        (Outreach.ActivityType.MATERIAL_SENT, "One-page overview or deck sent"),
        (Outreach.ActivityType.INBOUND_RESPONSE, "Prospect response"),
        (Outreach.ActivityType.CLOSE_LOOP, "Close-the-loop message"),
        (Outreach.ActivityType.MEETING_HANDOFF, "Founder meeting handoff"),
    ]
    LINKEDIN_ACTIVITY_CHOICES = [
        (Outreach.ActivityType.CONNECTION_REQUEST, "Connection request sent"),
        (Outreach.ActivityType.CONNECTION_ACCEPTED, "Connection accepted"),
        (Outreach.ActivityType.INITIAL_OUTREACH, "Proposition and one-page overview sent"),
        (Outreach.ActivityType.FOLLOW_UP, "Entry-point follow-up"),
        (Outreach.ActivityType.CLOSE_LOOP, "Close-the-loop message"),
        (Outreach.ActivityType.INBOUND_RESPONSE, "Prospect response"),
        (Outreach.ActivityType.FOUNDER_ESCALATION, "Escalated to founder"),
        (Outreach.ActivityType.MEETING_HANDOFF, "Founder meeting handoff"),
    ]

    class Meta:
        model = Outreach
        fields = ["activity_type", "medium", "outreach_date", "response"]
        widgets = {
            "outreach_date": DateInput(),
            "response": forms.Textarea(attrs={"rows": 3, "placeholder": "Optional: note the prospect's response"}),
        }

    def __init__(self, *args, prospect=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.prospect = prospect or getattr(self.instance, "prospect", None)
        self.fields["response"].label = "Response or factual notes"
        if self.prospect and self.prospect.workstream == Prospect.Workstream.LINKEDIN_OUTREACH:
            self.fields["activity_type"].choices = self.LINKEDIN_ACTIVITY_CHOICES
            self.fields["medium"].initial = Outreach.Medium.LINKEDIN
            self.fields["medium"].disabled = True
            if not self.is_bound and not self.instance.pk:
                next_activity = {
                    Prospect.LinkedInConnectionStatus.NOT_SENT: Outreach.ActivityType.CONNECTION_REQUEST,
                    Prospect.LinkedInConnectionStatus.REQUEST_SENT: Outreach.ActivityType.CONNECTION_ACCEPTED,
                    Prospect.LinkedInConnectionStatus.ACCEPTED: Outreach.ActivityType.INITIAL_OUTREACH,
                    Prospect.LinkedInConnectionStatus.DECLINED: Outreach.ActivityType.INBOUND_RESPONSE,
                }.get(self.prospect.linkedin_connection_status, Outreach.ActivityType.CONNECTION_REQUEST)
                if (
                    next_activity == Outreach.ActivityType.INITIAL_OUTREACH
                    and self.prospect.outreaches.filter(activity_type=Outreach.ActivityType.INITIAL_OUTREACH).exists()
                ):
                    next_activity = Outreach.ActivityType.FOLLOW_UP
                self.fields["activity_type"].initial = next_activity
        else:
            self.fields["activity_type"].choices = self.STANDARD_ACTIVITY_CHOICES
            if self.prospect and self.prospect.outreaches.exists() and not self.is_bound and not self.instance.pk:
                self.fields["activity_type"].initial = Outreach.ActivityType.FOLLOW_UP
        for field in self.fields.values():
            field.widget.attrs.setdefault("class", "form-control")

    def clean_outreach_date(self):
        outreach_date = self.cleaned_data["outreach_date"]
        if outreach_date > timezone.localdate():
            raise forms.ValidationError("Outreach date cannot be in the future.")
        return outreach_date

    def clean_medium(self):
        medium = self.cleaned_data["medium"]
        prospect = self.prospect
        if not prospect:
            return medium
        required_fields = {
            Outreach.Medium.EMAIL: (prospect.contact_email, "Add the contact email before recording an email outreach."),
            Outreach.Medium.PHONE: (prospect.contact_phone, "Add the contact phone number before recording a phone outreach."),
            Outreach.Medium.LINKEDIN: (
                prospect.contact_linkedin_url,
                "Add the contact LinkedIn page before recording a LinkedIn outreach.",
            ),
        }
        value, message = required_fields[medium]
        if not value:
            raise forms.ValidationError(message)
        return medium

    def clean(self):
        cleaned_data = super().clean()
        if (
            self.prospect
            and self.prospect.workstream == Prospect.Workstream.LINKEDIN_OUTREACH
            and not self.instance.pk
        ):
            activity_type = cleaned_data.get("activity_type")
            connection_status = self.prospect.linkedin_connection_status
            if (
                activity_type == Outreach.ActivityType.CONNECTION_REQUEST
                and connection_status != Prospect.LinkedInConnectionStatus.NOT_SENT
            ):
                self.add_error("activity_type", "A connection request is already recorded for this prospect.")
            elif (
                activity_type == Outreach.ActivityType.CONNECTION_ACCEPTED
                and connection_status != Prospect.LinkedInConnectionStatus.REQUEST_SENT
            ):
                self.add_error("activity_type", "Record acceptance only after the connection request is sent.")
            elif (
                activity_type not in {
                    Outreach.ActivityType.CONNECTION_REQUEST,
                    Outreach.ActivityType.CONNECTION_ACCEPTED,
                }
                and not (
                    activity_type == Outreach.ActivityType.INBOUND_RESPONSE
                    and connection_status == Prospect.LinkedInConnectionStatus.DECLINED
                )
                and connection_status != Prospect.LinkedInConnectionStatus.ACCEPTED
            ):
                self.add_error(
                    "activity_type",
                    "Record that the LinkedIn connection was accepted before sending post-acceptance messages.",
                )
        return cleaned_data


class DashboardFilterForm(forms.Form):
    DUE_CHOICES = [
        ("", "All action dates"),
        ("today", "Actions for today"),
        ("overdue", "Overdue actions"),
        ("upcoming", "Upcoming actions"),
        ("none", "No action date"),
    ]
    CLAIM_STATUS_CHOICES = [
        ("", "All claim statuses"),
        ("claimed", "Claimed prospects"),
        ("unclaimed", "Unclaimed prospects"),
    ]
    OUTREACH_CHOICES = [("", "Any outreach count")] + [(str(value), str(value)) for value in range(6)]

    search = forms.CharField(required=False, label="Search", widget=forms.TextInput(attrs={"placeholder": "Company or contact"}))
    due = forms.ChoiceField(required=False, choices=DUE_CHOICES, label="Action date")
    status = forms.ChoiceField(
        required=False,
        choices=[("", "All statuses")] + list(Prospect.Status.choices),
    )
    stage = forms.ChoiceField(
        required=False,
        choices=[("", "All playbook stages")] + list(Prospect.Stage.choices),
        label="Playbook stage",
    )
    claim_status = forms.ChoiceField(
        required=False,
        choices=CLAIM_STATUS_CHOICES,
        label="Claim status",
    )
    workstream = forms.ChoiceField(
        required=False,
        choices=[("", "All user classes")] + list(Prospect.Workstream.choices),
        label="User class",
    )
    outreach_count = forms.ChoiceField(required=False, choices=OUTREACH_CHOICES, label="Number of outreaches")
    owner = forms.ModelChoiceField(queryset=get_user_model().objects.none(), required=False, empty_label="All outreach users")

    def __init__(self, *args, user, **kwargs):
        super().__init__(*args, **kwargs)
        if is_manager(user):
            self.fields["owner"].queryset = (
                get_user_model()
                .objects.filter(profile__role__in=Prospect.Workstream.values, is_active=True)
                .select_related("profile")
                .order_by("profile__role", "first_name", "email")
            )
            self.fields["owner"].label_from_instance = lambda operator: (
                f"{operator.get_full_name() or operator.email} - {operator.profile.get_role_display()}"
            )
        else:
            self.fields.pop("owner")
            self.fields.pop("workstream")
        for field in self.fields.values():
            field.widget.attrs.setdefault("class", "filter-control")


class UserProvisionForm(forms.Form):
    full_name = forms.CharField(max_length=250, label="Name")
    email = forms.EmailField(max_length=254)
    role = forms.ChoiceField(choices=Profile.Role.choices, label="Class of user")

    def __init__(self, *args, instance=None, **kwargs):
        self.instance = instance
        initial = kwargs.setdefault("initial", {})
        if instance:
            initial.update(
                {
                    "full_name": instance.get_full_name(),
                    "email": instance.email,
                    "role": instance.profile.role,
                }
            )
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs.setdefault("class", "form-control")

    def clean_email(self):
        email = self.cleaned_data["email"].strip().lower()
        users = get_user_model().objects.filter(email__iexact=email)
        if self.instance:
            users = users.exclude(pk=self.instance.pk)
        if users.exists():
            raise forms.ValidationError("A user with this email address already exists.")
        return email

    def save(self):
        full_name = " ".join(self.cleaned_data["full_name"].split())
        name_parts = full_name.split(" ", 1)
        first_name = name_parts[0]
        last_name = name_parts[1] if len(name_parts) > 1 else ""
        email = self.cleaned_data["email"]
        old_email = self.instance.email.lower() if self.instance else ""
        if self.instance:
            user = self.instance
        else:
            base_username = email[:140]
            username = base_username
            suffix = 1
            while get_user_model().objects.filter(username=username).exists():
                suffix += 1
                username = f"{base_username[:145 - len(str(suffix))]}-{suffix}"
            user = get_user_model()(username=username)
            user.set_unusable_password()
        user.first_name = first_name
        user.last_name = last_name
        user.email = email
        if not self.instance:
            user.is_active = True
        if old_email and old_email != email:
            user.set_unusable_password()
        user.save()
        if old_email and old_email != email:
            user.socialaccount_set.all().delete()
        profile, _ = Profile.objects.get_or_create(user=user)
        profile.role = self.cleaned_data["role"]
        profile.save(update_fields=["role"])
        email_address = EmailAddress.objects.filter(user=user, primary=True).first()
        if email_address:
            email_address.email = email
            email_address.verified = False
            email_address.save(update_fields=["email", "verified"])
        else:
            EmailAddress.objects.create(user=user, email=email, verified=False, primary=True)
        return user
