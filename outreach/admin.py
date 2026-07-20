from django.contrib import admin

from .models import Outreach, Profile, Prospect


@admin.register(Profile)
class ProfileAdmin(admin.ModelAdmin):
    list_display = ("user", "role")
    list_filter = ("role",)
    search_fields = ("user__email", "user__first_name", "user__last_name")


class OutreachInline(admin.TabularInline):
    model = Outreach
    extra = 0
    max_num = 5


@admin.register(Prospect)
class ProspectAdmin(admin.ModelAdmin):
    list_display = ("company_name", "workstream", "owner", "stage", "import_source", "status", "next_action_date", "updated_at")
    list_filter = ("workstream", "stage", "import_source", "status", "owner")
    search_fields = ("company_name", "contact_name", "contact_email")
    date_hierarchy = "created_at"
    inlines = [OutreachInline]


@admin.register(Outreach)
class OutreachAdmin(admin.ModelAdmin):
    list_display = ("prospect", "sequence_number", "activity_type", "medium", "outreach_date", "recorded_by")
    list_filter = ("activity_type", "medium", "outreach_date")
    search_fields = ("prospect__company_name", "response")
