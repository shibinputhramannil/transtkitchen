from django.contrib import admin

from apps.inventory.admin import ReadOnlyAdminMixin

from .models import PrepPlan, PrepPlanIngredient, PrepPlanLine

LINE_FIELDS = ["menu_item", "forecast", "method", "suggested", "planned", "edited", "note"]
SNAPSHOT_FIELDS = ["ingredient", "unit", "required", "on_hand", "expiring_before", "usable", "shortfall"]


class PrepPlanLineInline(ReadOnlyAdminMixin, admin.TabularInline):
    model = PrepPlanLine
    fields = LINE_FIELDS
    readonly_fields = LINE_FIELDS
    extra = 0


class PrepPlanIngredientInline(ReadOnlyAdminMixin, admin.TabularInline):
    model = PrepPlanIngredient
    fields = SNAPSHOT_FIELDS
    readonly_fields = SNAPSHOT_FIELDS
    extra = 0
    verbose_name_plural = "ingredient snapshot (saved when finalized)"


@admin.register(PrepPlan)
class PrepPlanAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    """Plans change only through the API, which checks versions and dates."""

    list_display = ["plan_date", "organization", "status", "buffer_percent", "finalized_at"]
    list_filter = ["status"]
    list_select_related = ["organization"]
    search_fields = ["organization__name"]
    inlines = [PrepPlanLineInline, PrepPlanIngredientInline]
