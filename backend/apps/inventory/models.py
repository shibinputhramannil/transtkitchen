import uuid
from decimal import Decimal

from django.conf import settings
from django.db import models
from django.db.models.functions import Lower

from apps.core.models import TimeStampedModel
from apps.organizations.models import Organization

# Quantities are exact decimals: up to 999,999,999.999 in the ingredient's unit.
QUANTITY_DIGITS = 12
QUANTITY_PLACES = 3
MAX_QUANTITY = Decimal("999999999.999")


class Unit(models.TextChoices):
    GRAM = "g", "grams"
    KILOGRAM = "kg", "kilograms"
    MILLILITRE = "ml", "millilitres"
    LITRE = "l", "litres"
    EACH = "each", "each"


class Ingredient(TimeStampedModel):
    """
    Something the kitchen stocks. All of its quantities are in `unit`.

    quantity_on_hand changes only through stock movements, which lock this row
    while they update it.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name="ingredients"
    )
    name = models.CharField(max_length=120)
    unit = models.CharField(max_length=10, choices=Unit.choices)
    quantity_on_hand = models.DecimalField(
        max_digits=QUANTITY_DIGITS, decimal_places=QUANTITY_PLACES, default=Decimal("0")
    )
    # Stock at or below this level counts as low. Empty means no alert.
    reorder_level = models.DecimalField(
        max_digits=QUANTITY_DIGITS, decimal_places=QUANTITY_PLACES, null=True, blank=True
    )
    # Archived ingredients are hidden and cannot change stock, but keep their history.
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = [Lower("name")]
        constraints = [
            models.UniqueConstraint(
                "organization", Lower("name"), name="inventory_ingredient_unique_name"
            ),
            models.CheckConstraint(
                condition=models.Q(quantity_on_hand__gte=0),
                name="inventory_ingredient_on_hand_not_negative",
            ),
            models.CheckConstraint(
                condition=models.Q(reorder_level__isnull=True) | models.Q(reorder_level__gte=0),
                name="inventory_ingredient_reorder_level_not_negative",
            ),
            models.CheckConstraint(
                condition=models.Q(unit__in=Unit.values), name="inventory_ingredient_unit_valid"
            ),
        ]

    def __str__(self):
        return self.name

    @property
    def is_low_stock(self):
        return self.reorder_level is not None and self.quantity_on_hand <= self.reorder_level


class MovementKind(models.TextChoices):
    RECEIVE = "receive", "Delivery received"
    COUNT = "count", "Stock count"


class StockMovement(TimeStampedModel):
    """
    One change to an ingredient's stock. Movements are never edited or deleted:
    together they are the ingredient's stock history.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    ingredient = models.ForeignKey(
        Ingredient, on_delete=models.PROTECT, related_name="movements"
    )
    kind = models.CharField(max_length=20, choices=MovementKind.choices)
    quantity_change = models.DecimalField(
        max_digits=QUANTITY_DIGITS, decimal_places=QUANTITY_PLACES
    )
    quantity_after = models.DecimalField(
        max_digits=QUANTITY_DIGITS, decimal_places=QUANTITY_PLACES
    )
    note = models.CharField(max_length=255, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+"
    )
    # Chosen by the client for each submission. Sending the same request again
    # returns the movement it created instead of changing stock twice.
    request_id = models.UUIDField()

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["ingredient", "request_id"], name="inventory_movement_unique_request"
            ),
            models.CheckConstraint(
                condition=models.Q(quantity_after__gte=0),
                name="inventory_movement_after_not_negative",
            ),
            # A count that confirms the stock is still worth recording.
            models.CheckConstraint(
                condition=~models.Q(quantity_change=0) | models.Q(kind=MovementKind.COUNT),
                name="inventory_movement_change_not_zero",
            ),
            models.CheckConstraint(
                condition=models.Q(kind__in=MovementKind.values), name="inventory_movement_kind_valid"
            ),
        ]

    def __str__(self):
        return f"{self.get_kind_display()}: {self.quantity_change} {self.ingredient.unit} of {self.ingredient}"

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValueError("Stock movements cannot be changed.")
        super().save(*args, **kwargs)
