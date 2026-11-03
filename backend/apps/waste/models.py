import uuid

from django.conf import settings
from django.db import models

from apps.core.models import TimeStampedModel
from apps.inventory.models import (
    QUANTITY_DIGITS,
    QUANTITY_PLACES,
    Ingredient,
    StockBatch,
    StockMovement,
    Unit,
)
from apps.menu.models import MenuItem
from apps.organizations.models import Organization


def quantity_field(**kwargs):
    return models.DecimalField(max_digits=QUANTITY_DIGITS, decimal_places=QUANTITY_PLACES, **kwargs)


class WasteReason(models.TextChoices):
    EXPIRED = "expired", "Expired"
    SPOILED = "spoiled", "Spoiled"
    DAMAGED = "damaged", "Damaged (dropped or burnt)"
    OVERPRODUCTION = "overproduction", "Overproduction"
    QUALITY = "quality", "Quality (returned or not to standard)"
    OTHER = "other", "Other"


class WasteEntry(TimeStampedModel):
    """
    Something thrown away: an amount of one ingredient (optionally from one
    batch), or portions of a menu item, whose recipe says which ingredients
    went with it. Entries are never edited; a mistaken entry is voided, which
    puts back exactly the stock it took.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name="waste_entries"
    )
    ingredient = models.ForeignKey(
        Ingredient, on_delete=models.PROTECT, null=True, blank=True, related_name="waste_entries"
    )
    menu_item = models.ForeignKey(
        MenuItem, on_delete=models.PROTECT, null=True, blank=True, related_name="waste_entries"
    )
    batch = models.ForeignKey(
        StockBatch, on_delete=models.PROTECT, null=True, blank=True, related_name="waste_entries"
    )
    # As entered: in a unit compatible with the ingredient's, or portions
    # ("each") of a menu item.
    quantity = quantity_field()
    unit = models.CharField(max_length=10, choices=Unit.choices)
    reason = models.CharField(max_length=20, choices=WasteReason.choices)
    # The kitchen-local business date the waste happened on.
    wasted_on = models.DateField()
    note = models.CharField(max_length=255, blank=True)
    # Chosen by the client per submission; sending it again returns this entry.
    request_id = models.UUIDField()
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+"
    )
    voided_at = models.DateTimeField(null=True, blank=True)
    voided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    void_reason = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ["-wasted_on", "-created_at"]
        indexes = [models.Index(fields=["organization", "wasted_on"], name="waste_entry_org_date")]
        constraints = [
            models.UniqueConstraint(fields=["organization", "request_id"], name="waste_entry_unique_request"),
            models.CheckConstraint(
                condition=models.Q(ingredient__isnull=False, menu_item__isnull=True)
                | models.Q(ingredient__isnull=True, menu_item__isnull=False),
                name="waste_entry_one_target",
            ),
            models.CheckConstraint(
                condition=models.Q(batch__isnull=True) | models.Q(ingredient__isnull=False),
                name="waste_entry_batch_needs_ingredient",
            ),
            # Menu items are wasted in portions.
            models.CheckConstraint(
                condition=models.Q(menu_item__isnull=True) | models.Q(unit=Unit.EACH),
                name="waste_entry_menu_item_portions",
            ),
            models.CheckConstraint(condition=models.Q(quantity__gt=0), name="waste_entry_quantity_positive"),
            models.CheckConstraint(condition=models.Q(unit__in=Unit.values), name="waste_entry_unit_valid"),
            models.CheckConstraint(
                condition=models.Q(reason__in=WasteReason.values), name="waste_entry_reason_valid"
            ),
            models.CheckConstraint(
                condition=models.Q(voided_at__isnull=True, voided_by__isnull=True, void_reason="")
                | (models.Q(voided_at__isnull=False) & ~models.Q(void_reason="")),
                name="waste_entry_void_fields",
            ),
        ]

    def __str__(self):
        target = self.ingredient or self.menu_item
        return f"{self.quantity} {self.unit} {target} ({self.reason}, {self.wasted_on})"


class WasteDeduction(models.Model):
    """
    What an entry wasted of one ingredient, in that ingredient's stock unit at
    the time (`unit`), what it took from stock, and what was missing. `reversal`
    is the stock change that put it back when the entry was voided.
    """

    entry = models.ForeignKey(WasteEntry, on_delete=models.CASCADE, related_name="deductions")
    ingredient = models.ForeignKey(Ingredient, on_delete=models.PROTECT, related_name="waste_deductions")
    unit = models.CharField(max_length=10, choices=Unit.choices)
    required = quantity_field()
    taken = quantity_field()
    shortfall = quantity_field()
    movement = models.OneToOneField(
        StockMovement, on_delete=models.PROTECT, null=True, blank=True, related_name="waste_deduction"
    )
    reversal = models.OneToOneField(
        StockMovement, on_delete=models.PROTECT, null=True, blank=True, related_name="waste_reversal"
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["entry", "ingredient"], name="waste_deduction_unique_ingredient"),
            models.CheckConstraint(
                condition=models.Q(taken__gte=0, shortfall__gte=0)
                & models.Q(required=models.F("taken") + models.F("shortfall"))
                & models.Q(required__gt=0),
                name="waste_deduction_adds_up",
            ),
            models.CheckConstraint(
                condition=models.Q(taken=0, movement__isnull=True)
                | models.Q(taken__gt=0, movement__isnull=False),
                name="waste_deduction_movement",
            ),
            models.CheckConstraint(
                condition=models.Q(reversal__isnull=True) | models.Q(movement__isnull=False),
                name="waste_deduction_reversal_needs_movement",
            ),
            models.CheckConstraint(condition=models.Q(unit__in=Unit.values), name="waste_deduction_unit_valid"),
        ]
