from django.contrib import admin

from .models import (
    CompanyUpdateAudit,
    Outreach,
    OutreachUpdateAudit,
    Profile,
    Prospect,
    ProspectImportBatch,
    ProspectUpdateAudit,
)


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
    list_display = ("company_name", "company_id", "workstream", "owner", "stage", "is_not_eligible", "prospect_sent", "import_source", "status", "next_action_date", "updated_at")
    list_filter = ("workstream", "stage", "is_not_eligible", "prospect_sent", "import_source", "status", "owner")
    search_fields = ("company_name", "contact_name", "contact_email")
    date_hierarchy = "created_at"
    inlines = [OutreachInline]


@admin.register(Outreach)
class OutreachAdmin(admin.ModelAdmin):
    list_display = ("prospect", "sequence_number", "activity_type", "medium", "outreach_date", "recorded_by")
    list_filter = ("activity_type", "medium", "outreach_date")
    search_fields = ("prospect__company_name", "response")


@admin.register(CompanyUpdateAudit)
class CompanyUpdateAuditAdmin(admin.ModelAdmin):
    list_display = ("company_id", "modified_by", "source", "created_at")
    list_filter = ("source", "created_at")
    search_fields = ("modified_by__email", "modified_by__first_name", "modified_by__last_name")
    readonly_fields = (
        "company",
        "modified_by",
        "source",
        "previous_values",
        "changed_values",
        "created_at",
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(ProspectUpdateAudit)
class ProspectUpdateAuditAdmin(admin.ModelAdmin):
    list_display = ("prospect", "modified_by", "source", "created_at")
    list_filter = ("source", "created_at")
    search_fields = (
        "prospect__company_name",
        "modified_by__email",
        "modified_by__first_name",
        "modified_by__last_name",
    )
    readonly_fields = (
        "prospect",
        "modified_by",
        "source",
        "previous_values",
        "changed_values",
        "created_at",
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(OutreachUpdateAudit)
class OutreachUpdateAuditAdmin(admin.ModelAdmin):
    list_display = ("outreach", "modified_by", "source", "created_at")
    list_filter = ("source", "created_at")
    search_fields = (
        "outreach__prospect__company_name",
        "modified_by__email",
        "modified_by__first_name",
        "modified_by__last_name",
    )
    readonly_fields = (
        "outreach",
        "modified_by",
        "source",
        "previous_values",
        "changed_values",
        "created_at",
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(ProspectImportBatch)
class ProspectImportBatchAdmin(admin.ModelAdmin):
    list_display = (
        "source_filename",
        "uploaded_by",
        "target_workstream",
        "created_count",
        "skipped_count",
        "status",
        "created_at",
    )
    list_filter = ("target_workstream", "status", "created_at")
    search_fields = ("source_filename", "uploaded_by__email")
    readonly_fields = (
        "id",
        "uploaded_by",
        "source_filename",
        "target_workstream",
        "owner",
        "total_rows",
        "created_count",
        "skipped_count",
        "skipped_rows",
        "created_rows",
        "status",
        "rolled_back_count",
        "rollback_skipped_rows",
        "created_at",
        "rolled_back_at",
        "rolled_back_by",
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
