# core/admin.py
from django.contrib import admin
from .models import SiteSettings, FileBatch, FileItem, Work, WorkClaim


# -------- SiteSettings (singleton-style) --------
@admin.register(SiteSettings)
class SiteSettingsAdmin(admin.ModelAdmin):
    # Use only real fields, and add a computed display_name column
    list_display = ("id", "display_name", "openai_api_key", "youtube_api_key")
    search_fields = ("openai_api_key", "youtube_api_key")

    def display_name(self, obj):
        # Show whichever exists, else a friendly label
        return getattr(obj, "name", None) or getattr(obj, "site_name", None) or "Site Settings"
    display_name.short_description = "Name"

    # Optional: make it behave like a singleton settings row
    def has_add_permission(self, request):
        # allow add only if no rows exist
        return not SiteSettings.objects.exists() or super().has_add_permission(request)


# -------- FileBatch / FileItem --------
class FileItemInline(admin.TabularInline):
    model = FileItem
    extra = 0
    readonly_fields = ("id", "description", "tags", "reuse_limit", "used_count")


@admin.register(FileBatch)
class FileBatchAdmin(admin.ModelAdmin):
    list_display = ("id", "file_name", "seed_keyword", "title_count", "suggest_count", "desc_length", "created_at")
    inlines = [FileItemInline]
    search_fields = ("file_name", "seed_keyword")
    list_filter = ("created_at",)


# -------- Work / WorkClaim --------
class WorkClaimInline(admin.TabularInline):
    model = WorkClaim
    extra = 0
    readonly_fields = (
        "file_item", "title", "status", "client_id",
        "assigned_at", "expires_at", "submitted_at", "youtube_url",
    )


@admin.register(Work)
class WorkAdmin(admin.ModelAdmin):
    # Keep "name" here only if Work actually has a 'name' field. If not, remove it.
    list_display = ("id", "name", "file_batch", "remaining_slots", "total_slots",
                    "price_per_item", "deadline_minutes", "created_at")
    inlines = [WorkClaimInline]
    search_fields = ("name",)
    list_filter = ("file_batch", "created_at")


@admin.register(WorkClaim)
class WorkClaimAdmin(admin.ModelAdmin):
    list_display = ("id", "work", "file_item", "status", "client_id",
                    "assigned_at", "expires_at", "submitted_at")
    search_fields = ("client_id", "title", "youtube_url")
    list_filter = ("status", "work")


from .models import MilestoneRule, MilestonePayout

@admin.register(MilestoneRule)
class MilestoneRuleAdmin(admin.ModelAdmin):
    list_display = ("threshold_views", "payout_amount", "active", "created_at")
    list_filter  = ("active",)
    search_fields = ("threshold_views", "payout_amount")

@admin.register(MilestonePayout)
class MilestonePayoutAdmin(admin.ModelAdmin):
    list_display = ("id", "claim", "rule", "amount", "status", "views_snapshot", "likes_snapshot", "created_at")
    list_filter  = ("status", "rule")
    search_fields = ("claim__user__email", "claim__work__name")
