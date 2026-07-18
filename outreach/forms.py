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


class ProspectForm(forms.ModelForm):
    class Meta:
        model = Prospect
        fields = [
            "owner",
            "company_name",
            "website",
            "short_description",
            "contact_name",
            "contact_linkedin_url",
            "contact_email",
            "contact_phone",
            "next_action",
            "next_action_date",
            "status",
            "meeting_scheduled_at",
            "comments",
        ]
        widgets = {
            "short_description": forms.Textarea(attrs={"rows": 3}),
            "next_action_date": DateInput(),
            "meeting_scheduled_at": DateTimeLocalInput(format="%Y-%m-%dT%H:%M"),
            "comments": forms.Textarea(attrs={"rows": 4}),
        }
        help_texts = {
            "short_description": "What the company does and why it may be relevant.",
            "next_action": "Example: Send product brief or call procurement lead.",
            "meeting_scheduled_at": "Required when status is Meeting scheduled.",
        }

    def __init__(self, *args, user, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user
        self.fields["meeting_scheduled_at"].input_formats = ["%Y-%m-%dT%H:%M"]
        if is_manager(user):
            owner_query = Q(profile__role=Profile.Role.INTERN)
            if self.instance and self.instance.pk:
                owner_query |= Q(pk=self.instance.owner_id)
            self.fields["owner"].queryset = (
                get_user_model().objects.filter(owner_query, is_active=True).select_related("profile").order_by("first_name", "email")
            )
            self.fields["owner"].label = "Assigned intern"
        else:
            self.fields.pop("owner")

        for name, field in self.fields.items():
            field.widget.attrs.setdefault("class", "form-control")
            if field.required:
                field.widget.attrs["aria-required"] = "true"

    def save(self, commit=True):
        prospect = super().save(commit=False)
        if not is_manager(self.user):
            prospect.owner = self.user
        if commit:
            prospect.save()
        return prospect


class OutreachForm(forms.ModelForm):
    class Meta:
        model = Outreach
        fields = ["medium", "outreach_date", "response"]
        widgets = {
            "outreach_date": DateInput(),
            "response": forms.Textarea(attrs={"rows": 3, "placeholder": "Optional: note the prospect's response"}),
        }

    def __init__(self, *args, prospect=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.prospect = prospect or getattr(self.instance, "prospect", None)
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


class DashboardFilterForm(forms.Form):
    DUE_CHOICES = [
        ("", "All action dates"),
        ("today", "Actions for today"),
        ("overdue", "Overdue actions"),
        ("upcoming", "Upcoming actions"),
        ("none", "No action date"),
    ]
    OUTREACH_CHOICES = [("", "Any outreach count")] + [(str(value), str(value)) for value in range(6)]

    search = forms.CharField(required=False, label="Search", widget=forms.TextInput(attrs={"placeholder": "Company or contact"}))
    due = forms.ChoiceField(required=False, choices=DUE_CHOICES, label="Action date")
    status = forms.ChoiceField(
        required=False,
        choices=[("", "All statuses")] + list(Prospect.Status.choices),
    )
    outreach_count = forms.ChoiceField(required=False, choices=OUTREACH_CHOICES, label="Number of outreaches")
    owner = forms.ModelChoiceField(queryset=get_user_model().objects.none(), required=False, empty_label="All interns")

    def __init__(self, *args, user, **kwargs):
        super().__init__(*args, **kwargs)
        if is_manager(user):
            self.fields["owner"].queryset = (
                get_user_model().objects.filter(profile__role=Profile.Role.INTERN, is_active=True).order_by("first_name", "email")
            )
        else:
            self.fields.pop("owner")
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
