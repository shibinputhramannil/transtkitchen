from django.contrib import admin

from apps.inventory.admin import ReadOnlyAdminMixin

from .models import WasteDeduction, WasteEntry

DEDUCTION_FIELDS = ["ingredient", "unit", "required", "taken", "shortfall", "movement", "reversal"]


class WasteDeductionInline(ReadOnlyAdminMixin, admin.TabularInline):
    model = WasteDeduction
    fields = DEDUCTION_FIELDS
    readonly_fields = DEDUCTION_FIELDS
    extra = 0


@admin.register(WasteEntry)
class WasteEntryAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    """Waste changes only through the API, which keeps stock consistent."""

    list_display = ["wasted_on", "organization", "ingredient", "menu_item", "quantity", "unit", "reason", "voided_at"]
    list_filter = ["reason"]
    list_select_related = ["organization", "ingredient", "menu_item"]
    search_fields = ["ingredient__name", "menu_item__name", "organization__name", "note"]
    inlines = [WasteDeductionInline]
