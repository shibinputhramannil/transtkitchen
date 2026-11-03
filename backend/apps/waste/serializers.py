from decimal import Decimal

from rest_framework import serializers

from apps.inventory.models import MAX_QUANTITY, QUANTITY_DIGITS, QUANTITY_PLACES, Ingredient, Unit
from apps.inventory.units import compatible, convert, round_quantity
from apps.menu.models import MenuItem

from .models import WasteEntry, WasteReason
from .services import ACKNOWLEDGEABLE


class WasteLogSerializer(serializers.Serializer):
    """
    Needs "organization" in its context: the ingredient, menu item, and batch
    must belong to it. Ingredient waste is in a unit compatible with the
    ingredient's (default: its own); menu-item waste is in portions ("each").
    """

    request_id = serializers.UUIDField()
    ingredient = serializers.UUIDField(required=False)
    menu_item = serializers.UUIDField(required=False)
    batch = serializers.UUIDField(required=False)
    quantity = serializers.DecimalField(
        max_digits=QUANTITY_DIGITS,
        decimal_places=QUANTITY_PLACES,
        min_value=Decimal("0.001"),
        max_value=MAX_QUANTITY,
    )
    unit = serializers.ChoiceField(choices=Unit.choices, required=False)
    reason = serializers.ChoiceField(choices=WasteReason.choices)
    # The date range is checked after retries are recognised.
    wasted_on = serializers.DateField(required=False)
    note = serializers.CharField(max_length=255, required=False, default="", allow_blank=True)
    acknowledged = serializers.ListField(
        child=serializers.ChoiceField(choices=ACKNOWLEDGEABLE), required=False, default=list
    )

    def validate(self, attrs):
        organization = self.context["organization"]
        if bool(attrs.get("ingredient")) == bool(attrs.get("menu_item")):
            raise serializers.ValidationError("Give either an ingredient or a menu_item.")

        if attrs.get("menu_item"):
            if attrs.get("batch"):
                raise serializers.ValidationError({"batch": ["Batches belong to ingredients, not menu items."]})
            menu_item = MenuItem.objects.filter(organization=organization, pk=attrs["menu_item"]).first()
            if menu_item is None:
                raise serializers.ValidationError({"menu_item": ["No such menu item."]})
            if attrs.get("unit", Unit.EACH) != Unit.EACH:
                raise serializers.ValidationError({"unit": ["Menu items are wasted in portions (each)."]})
            attrs["menu_item"] = menu_item
            attrs["unit"] = Unit.EACH
            return attrs

        ingredient = Ingredient.objects.filter(organization=organization, pk=attrs["ingredient"]).first()
        if ingredient is None:
            raise serializers.ValidationError({"ingredient": ["No such ingredient."]})
        attrs["ingredient"] = ingredient
        unit = attrs.setdefault("unit", ingredient.unit)
        if not compatible(unit, ingredient.unit):
            raise serializers.ValidationError(
                {"unit": [f"{ingredient.name} is stocked in {ingredient.unit}; use a compatible unit."]}
            )
        if round_quantity(convert(attrs["quantity"], unit, ingredient.unit)) <= 0:
            raise serializers.ValidationError(
                {"quantity": [f"That is less than 0.001 {ingredient.unit}, the smallest amount recorded."]}
            )
        if attrs.get("batch"):
            batch = ingredient.batches.filter(pk=attrs["batch"]).first()
            if batch is None:
                raise serializers.ValidationError({"batch": ["No such batch of this ingredient."]})
            attrs["batch"] = batch
        return attrs


class VoidSerializer(serializers.Serializer):
    reason = serializers.CharField(max_length=255)


class WasteDeductionSerializer(serializers.Serializer):
    ingredient = serializers.SerializerMethodField()
    unit = serializers.CharField()
    required = serializers.DecimalField(max_digits=QUANTITY_DIGITS, decimal_places=QUANTITY_PLACES)
    taken = serializers.DecimalField(max_digits=QUANTITY_DIGITS, decimal_places=QUANTITY_PLACES)
    shortfall = serializers.DecimalField(max_digits=QUANTITY_DIGITS, decimal_places=QUANTITY_PLACES)
    returned = serializers.SerializerMethodField()

    def get_ingredient(self, deduction):
        return {"id": str(deduction.ingredient_id), "name": deduction.ingredient.name}

    def get_returned(self, deduction):
        return deduction.reversal_id is not None


class WasteEntrySerializer(serializers.ModelSerializer):
    kind = serializers.SerializerMethodField()
    ingredient = serializers.SerializerMethodField()
    menu_item = serializers.SerializerMethodField()
    batch = serializers.SerializerMethodField()
    created_by = serializers.SlugRelatedField(slug_field="email", read_only=True)
    voided_by = serializers.SlugRelatedField(slug_field="email", read_only=True)
    deductions = WasteDeductionSerializer(many=True, read_only=True)

    class Meta:
        model = WasteEntry
        fields = [
            "id",
            "kind",
            "ingredient",
            "menu_item",
            "batch",
            "quantity",
            "unit",
            "reason",
            "wasted_on",
            "note",
            "deductions",
            "created_by",
            "created_at",
            "voided_at",
            "voided_by",
            "void_reason",
        ]
        read_only_fields = fields

    def get_kind(self, entry):
        return "ingredient" if entry.ingredient_id else "menu_item"

    def get_ingredient(self, entry):
        if entry.ingredient is None:
            return None
        return {"id": str(entry.ingredient.pk), "name": entry.ingredient.name, "unit": entry.ingredient.unit}

    def get_menu_item(self, entry):
        if entry.menu_item is None:
            return None
        return {"id": str(entry.menu_item.pk), "name": entry.menu_item.name}

    def get_batch(self, entry):
        if entry.batch is None:
            return None
        return {"id": str(entry.batch.pk), "expires_on": entry.batch.expires_on}
