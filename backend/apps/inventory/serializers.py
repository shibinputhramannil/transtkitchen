from decimal import Decimal

from rest_framework import serializers

from .models import MAX_QUANTITY, QUANTITY_DIGITS, QUANTITY_PLACES, Ingredient, StockMovement

DUPLICATE_NAME = "An ingredient with this name already exists."


def quantity_field(**kwargs):
    return serializers.DecimalField(
        max_digits=QUANTITY_DIGITS,
        decimal_places=QUANTITY_PLACES,
        max_value=MAX_QUANTITY,
        **kwargs,
    )


class IngredientSerializer(serializers.ModelSerializer):
    reorder_level = quantity_field(min_value=Decimal("0"), required=False, allow_null=True)
    is_low_stock = serializers.BooleanField(read_only=True)

    class Meta:
        model = Ingredient
        fields = [
            "id",
            "name",
            "unit",
            "quantity_on_hand",
            "reorder_level",
            "is_low_stock",
            "is_active",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "quantity_on_hand", "created_at", "updated_at"]

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


class StockMovementSerializer(serializers.ModelSerializer):
    created_by = serializers.SlugRelatedField(slug_field="email", read_only=True)

    class Meta:
        model = StockMovement
        fields = [
            "id",
            "kind",
            "quantity_change",
            "quantity_after",
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


class CountSerializer(StockChangeSerializer):
    counted_quantity = quantity_field(min_value=Decimal("0"))
    # The stock level the counter started from; a mismatch means stock changed meanwhile.
    expected_quantity = quantity_field(min_value=Decimal("0"), required=False)
