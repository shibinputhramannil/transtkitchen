from decimal import Decimal

from rest_framework import serializers

from .models import (
    MAX_QUANTITY,
    QUANTITY_DIGITS,
    QUANTITY_PLACES,
    Ingredient,
    MovementLine,
    StockBatch,
    StockMovement,
)
from .stock import expiry_status

DUPLICATE_NAME = "An ingredient with this name already exists."


def quantity_field(**kwargs):
    return serializers.DecimalField(
        max_digits=QUANTITY_DIGITS,
        decimal_places=QUANTITY_PLACES,
        max_value=MAX_QUANTITY,
        **kwargs,
    )


class IngredientSerializer(serializers.ModelSerializer):
    """
    Needs "organization" and "today" (the organization's local date) in its
    context. next_expiry and expired_quantity come from batches with stock.
    """

    reorder_level = quantity_field(min_value=Decimal("0"), required=False, allow_null=True)
    is_low_stock = serializers.BooleanField(read_only=True)
    next_expiry = serializers.DateField(read_only=True)
    expiry_status = serializers.SerializerMethodField()
    expired_quantity = quantity_field(read_only=True)

    class Meta:
        model = Ingredient
        fields = [
            "id",
            "name",
            "unit",
            "quantity_on_hand",
            "reorder_level",
            "is_low_stock",
            "next_expiry",
            "expiry_status",
            "expired_quantity",
            "is_active",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "quantity_on_hand", "created_at", "updated_at"]

    def to_representation(self, instance):
        if not hasattr(instance, "next_expiry"):
            # Fresh from a create or an edit, so not annotated yet.
            instance = Ingredient.objects.with_expiry(self.context["today"]).get(pk=instance.pk)
        return super().to_representation(instance)

    def get_expiry_status(self, ingredient):
        if ingredient.next_expiry is None:
            return None
        return expiry_status(ingredient.next_expiry, self.context["today"])

    def validate_name(self, value):
        # The view supplies the organization; names are unique within it, ignoring case.
        others = Ingredient.objects.filter(
            organization=self.context["organization"], name__iexact=value
        )
        if self.instance is not None:
            others = others.exclude(pk=self.instance.pk)
        if others.exists():
            raise serializers.ValidationError(DUPLICATE_NAME)
        return value


class StockBatchSerializer(serializers.ModelSerializer):
    """Needs "today" (the organization's local date) in its context."""

    status = serializers.SerializerMethodField()

    class Meta:
        model = StockBatch
        fields = [
            "id",
            "source",
            "quantity_received",
            "quantity_remaining",
            "expires_on",
            "received_at",
            "status",
        ]
        read_only_fields = fields

    def get_status(self, batch):
        return expiry_status(batch.expires_on, self.context["today"])


class ExpiringIngredientSerializer(serializers.ModelSerializer):
    class Meta:
        model = Ingredient
        fields = ["id", "name", "unit", "is_active"]


class ExpiringBatchSerializer(StockBatchSerializer):
    ingredient = ExpiringIngredientSerializer(read_only=True)

    class Meta(StockBatchSerializer.Meta):
        fields = [*StockBatchSerializer.Meta.fields, "ingredient"]
        read_only_fields = fields


class MovementLineSerializer(serializers.ModelSerializer):
    expires_on = serializers.DateField(source="batch.expires_on", read_only=True)

    class Meta:
        model = MovementLine
        fields = ["batch", "quantity_change", "expires_on"]
        read_only_fields = fields


class StockMovementSerializer(serializers.ModelSerializer):
    created_by = serializers.SlugRelatedField(slug_field="email", read_only=True)
    lines = MovementLineSerializer(many=True, read_only=True)

    class Meta:
        model = StockMovement
        fields = [
            "id",
            "kind",
            "quantity_change",
            "quantity_after",
            "expires_on",
            "corrected_batch",
            "lines",
            "note",
            "created_by",
            "created_at",
        ]
        read_only_fields = fields


class StockChangeSerializer(serializers.Serializer):
    request_id = serializers.UUIDField()
    note = serializers.CharField(max_length=255, required=False, default="", allow_blank=True)


class ReceiveSerializer(StockChangeSerializer):
    quantity = quantity_field(min_value=Decimal("0.001"))
    # Optional; the date range is checked by the view, after retries are recognised.
    expires_on = serializers.DateField(required=False, allow_null=True, default=None)


class CountSerializer(StockChangeSerializer):
    counted_quantity = quantity_field(min_value=Decimal("0"))
    # The stock level the counter started from; a mismatch means stock changed meanwhile.
    expected_quantity = quantity_field(min_value=Decimal("0"), required=False)


class ExpiryCorrectionSerializer(serializers.Serializer):
    # Required, but may be null to record that the batch has no expiry date.
    expires_on = serializers.DateField(allow_null=True)
    note = serializers.CharField(max_length=200, required=False, default="", allow_blank=True)
