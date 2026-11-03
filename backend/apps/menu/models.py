import uuid

from django.core.exceptions import ValidationError
from django.db import models
from django.db.models.functions import Lower

from apps.core.models import TimeStampedModel
from apps.inventory.models import QUANTITY_DIGITS, QUANTITY_PLACES, Ingredient, Unit
from apps.inventory.units import compatible
from apps.organizations.models import Organization


class MenuItem(TimeStampedModel):
    """Something the kitchen sells. Its recipe says which ingredients one portion uses."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name="menu_items"
    )
    name = models.CharField(max_length=150)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = [Lower("name")]
        constraints = [
            models.UniqueConstraint("organization", Lower("name"), name="menu_item_unique_name"),
        ]

    def __str__(self):
        return self.name


class RecipeLine(models.Model):
    """
    How much of one ingredient one portion of a menu item uses. The unit may
    differ from the ingredient's (grams of an ingredient stocked in kilograms)
    but must measure the same thing.
    """

    menu_item = models.ForeignKey(MenuItem, on_delete=models.CASCADE, related_name="recipe_lines")
    ingredient = models.ForeignKey(Ingredient, on_delete=models.PROTECT, related_name="recipe_lines")
    quantity = models.DecimalField(max_digits=QUANTITY_DIGITS, decimal_places=QUANTITY_PLACES)
    unit = models.CharField(max_length=10, choices=Unit.choices)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["menu_item", "ingredient"], name="menu_recipe_unique_ingredient"),
            models.CheckConstraint(condition=models.Q(quantity__gt=0), name="menu_recipe_quantity_positive"),
            models.CheckConstraint(condition=models.Q(unit__in=Unit.values), name="menu_recipe_unit_valid"),
        ]

    def __str__(self):
        return f"{self.quantity} {self.unit} {self.ingredient} in {self.menu_item}"

    def clean(self):
        # The API checks the same rules; this covers the Django admin.
        if self.ingredient_id and self.menu_item_id:
            if self.ingredient.organization_id != self.menu_item.organization_id:
                raise ValidationError({"ingredient": "The ingredient belongs to another organization."})
            if self.unit and not compatible(self.unit, self.ingredient.unit):
                raise ValidationError({"unit": f"Use a unit compatible with {self.ingredient.unit}."})


class SaleSource(models.TextChoices):
    CSV = "csv", "CSV file"


class MenuItemAlias(TimeStampedModel):
    """
    Remembers which menu item a name (or item ID) from a sales source means,
    so the next import matches it automatically.
    """

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="+")
    menu_item = models.ForeignKey(MenuItem, on_delete=models.CASCADE, related_name="aliases")
    source = models.CharField(max_length=20, choices=SaleSource.choices)
    external_name = models.CharField(max_length=200)
    external_id = models.CharField(max_length=100, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                "organization", "source", Lower("external_name"), name="menu_alias_unique_name"
            ),
            models.UniqueConstraint(
                fields=["organization", "source", "external_id"],
                condition=~models.Q(external_id=""),
                name="menu_alias_unique_external_id",
            ),
        ]

    def __str__(self):
        return f"{self.external_name} → {self.menu_item}"
