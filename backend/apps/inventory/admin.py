from django.contrib import admin

from .models import Ingredient, MovementLine, StockBatch, StockMovement

MOVEMENT_FIELDS = [
    "kind",
    "quantity_change",
    "quantity_after",
    "expires_on",
    "note",
    "created_by",
    "created_at",
    "request_id",
]
BATCH_FIELDS = [
    "source",
    "quantity_received",
    "quantity_remaining",
    "expires_on",
    "received_at",
    "source_movement",
]
LINE_FIELDS = ["batch", "quantity_change"]


class ReadOnlyAdminMixin:
    """Stock history is view-only here too; it changes only through the API."""

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


class StockMovementInline(ReadOnlyAdminMixin, admin.TabularInline):
    model = StockMovement
    fk_name = "ingredient"
    fields = MOVEMENT_FIELDS
    readonly_fields = MOVEMENT_FIELDS
    extra = 0


class StockBatchInline(ReadOnlyAdminMixin, admin.TabularInline):
    model = StockBatch
    fields = BATCH_FIELDS
    readonly_fields = BATCH_FIELDS
    extra = 0


class MovementLineInline(ReadOnlyAdminMixin, admin.TabularInline):
    model = MovementLine
    fields = LINE_FIELDS
    readonly_fields = LINE_FIELDS
    extra = 0


@admin.register(Ingredient)
class IngredientAdmin(admin.ModelAdmin):
    list_display = ["name", "organization", "quantity_on_hand", "unit", "is_active"]
    list_filter = ["is_active", "unit"]
    list_select_related = ["organization"]
    search_fields = ["name", "organization__name"]
    autocomplete_fields = ["organization"]
    inlines = [StockBatchInline, StockMovementInline]

    def get_readonly_fields(self, request, obj=None):
        # Stock changes only through movements, and the unit is fixed once there are any.
        readonly = ["quantity_on_hand"]
        if obj is not None and obj.movements.exists():
            readonly.append("unit")
        return readonly


@admin.register(StockMovement)
class StockMovementAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    list_display = ["ingredient", "kind", "quantity_change", "quantity_after", "created_by", "created_at"]
    list_filter = ["kind"]
    list_select_related = ["ingredient", "created_by"]
    search_fields = ["ingredient__name", "ingredient__organization__name"]
    inlines = [MovementLineInline]


@admin.register(StockBatch)
class StockBatchAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    list_display = ["ingredient", "source", "quantity_remaining", "expires_on", "received_at"]
    list_filter = ["source"]
    list_select_related = ["ingredient"]
    search_fields = ["ingredient__name", "ingredient__organization__name"]
