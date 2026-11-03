from django.contrib import admin

from apps.inventory.admin import ReadOnlyAdminMixin

from .models import ImportDeduction, SaleLine, SalesImport

DEDUCTION_FIELDS = ["ingredient", "required", "taken", "shortfall", "movement"]


class ImportDeductionInline(ReadOnlyAdminMixin, admin.TabularInline):
    model = ImportDeduction
    fields = DEDUCTION_FIELDS
    readonly_fields = DEDUCTION_FIELDS
    extra = 0


@admin.register(SalesImport)
class SalesImportAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    """Imports change only through the API, which keeps stock consistent."""

    list_display = ["file_name", "organization", "status", "first_date", "last_date", "row_count", "created_at"]
    list_filter = ["status", "source"]
    list_select_related = ["organization"]
    search_fields = ["file_name", "organization__name"]
    inlines = [ImportDeductionInline]


@admin.register(SaleLine)
class SaleLineAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    list_display = ["sales_import", "row_number", "business_date", "item_name", "quantity", "menu_item", "error"]
    list_select_related = ["sales_import", "menu_item"]
    search_fields = ["item_name", "sales_import__file_name"]
