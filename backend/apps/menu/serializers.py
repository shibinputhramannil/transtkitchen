from decimal import Decimal

from rest_framework import serializers

from apps.inventory.models import MAX_QUANTITY, QUANTITY_DIGITS, QUANTITY_PLACES, Ingredient, Unit
from apps.inventory.units import compatible

from .models import MenuItem

DUPLICATE_NAME = "A menu item with this name already exists."
MAX_RECIPE_LINES = 50


class RecipeIngredientSerializer(serializers.ModelSerializer):
    class Meta:
        model = Ingredient
        fields = ["id", "name", "unit", "is_active"]


class RecipeLineSerializer(serializers.Serializer):
    ingredient = RecipeIngredientSerializer(read_only=True)
    quantity = serializers.DecimalField(
        max_digits=QUANTITY_DIGITS, decimal_places=QUANTITY_PLACES, read_only=True
    )
    unit = serializers.CharField(read_only=True)


class MenuItemSerializer(serializers.ModelSerializer):
    """Needs "organization" in its context."""

    recipe = RecipeLineSerializer(source="recipe_lines", many=True, read_only=True)

    class Meta:
        model = MenuItem
        fields = ["id", "name", "is_active", "recipe", "created_at", "updated_at"]
        read_only_fields = ["id", "created_at", "updated_at"]

    def validate_name(self, value):
        others = MenuItem.objects.filter(organization=self.context["organization"], name__iexact=value)
        if self.instance is not None:
            others = others.exclude(pk=self.instance.pk)
        if others.exists():
            raise serializers.ValidationError(DUPLICATE_NAME)
        return value


class RecipeLineInputSerializer(serializers.Serializer):
    ingredient = serializers.UUIDField()
    quantity = serializers.DecimalField(
        max_digits=QUANTITY_DIGITS,
        decimal_places=QUANTITY_PLACES,
        min_value=Decimal("0.001"),
        max_value=MAX_QUANTITY,
    )
    unit = serializers.ChoiceField(choices=Unit.choices)


class RecipeSerializer(serializers.Serializer):
    """
    Replaces a menu item's whole recipe. Needs "organization" in its context:
    ingredients must belong to it.
    """

    lines = RecipeLineInputSerializer(many=True, max_length=MAX_RECIPE_LINES)

    def validate_lines(self, lines):
        ids = [line["ingredient"] for line in lines]
        if len(set(ids)) != len(ids):
            raise serializers.ValidationError("Each ingredient may appear only once.")
        ingredients = Ingredient.objects.filter(organization=self.context["organization"], pk__in=ids).in_bulk()
        errors = []
        for line in lines:
            ingredient = ingredients.get(line["ingredient"])
            if ingredient is None:
                # Unknown here, including ingredients of other organizations.
                errors.append({"ingredient": ["No such ingredient."]})
            elif not compatible(line["unit"], ingredient.unit):
                errors.append(
                    {"unit": [f"{ingredient.name} is stocked in {ingredient.unit}; use a compatible unit."]}
                )
            else:
                line["ingredient"] = ingredient
                errors.append({})
        if any(errors):
            raise serializers.ValidationError(errors)
        return lines
