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


class IngredientQuerySet(models.QuerySet):
    def with_expiry(self, today):
        """Adds next_expiry and expired_quantity, from batches that still hold stock."""
        in_stock = models.Q(batches__quantity_remaining__gt=0)
        return self.annotate(
            next_expiry=models.Min("batches__expires_on", filter=in_stock),
            expired_quantity=models.Sum(
                "batches__quantity_remaining",
                filter=in_stock & models.Q(batches__expires_on__lt=today),
                default=Decimal("0"),
            ),
        )


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

    objects = IngredientQuerySet.as_manager()

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
    EXPIRY_CORRECTION = "expiry_correction", "Expiry date corrected"
    SALE = "sale", "Used by sales"


class StockMovement(TimeStampedModel):
    """
    One change to an ingredient's stock. Movements are never edited or deleted:
    together they are the ingredient's stock history.

    Movements recorded since batch tracking began (tracks_batches) say, through
    their lines, which batches they changed; the lines always add up to
    quantity_change. Earlier movements have no lines; their stock is held in
    the ingredient's opening batch.
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
    # returns the movement it created instead of changing stock twice. Expiry
    # corrections, which are naturally repeatable, have none.
    request_id = models.UUIDField(null=True, blank=True)
    tracks_batches = models.BooleanField(default=True)
    # For a delivery: the expiry date entered with it. For an expiry
    # correction: the new date. Empty means no expiry date.
    expires_on = models.DateField(null=True, blank=True)
    # For an expiry correction: the batch whose date changed.
    corrected_batch = models.ForeignKey(
        "StockBatch", on_delete=models.PROTECT, null=True, blank=True, related_name="corrections"
    )

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
            # A count that confirms the stock is still worth recording, and an
            # expiry correction changes no quantity at all.
            models.CheckConstraint(
                condition=~models.Q(quantity_change=0)
                | models.Q(kind__in=[MovementKind.COUNT, MovementKind.EXPIRY_CORRECTION]),
                name="inventory_movement_change_not_zero",
            ),
            models.CheckConstraint(
                condition=models.Q(kind__in=MovementKind.values), name="inventory_movement_kind_valid"
            ),
            # Deliveries and counts are retried with their request_id; corrections name their batch.
            models.CheckConstraint(
                condition=(
                    models.Q(
                        kind=MovementKind.EXPIRY_CORRECTION,
                        quantity_change=0,
                        corrected_batch__isnull=False,
                        request_id__isnull=True,
                    )
                    | (
                        ~models.Q(kind=MovementKind.EXPIRY_CORRECTION)
                        & models.Q(corrected_batch__isnull=True, request_id__isnull=False)
                    )
                ),
                name="inventory_movement_kind_fields",
            ),
        ]

    def __str__(self):
        return f"{self.get_kind_display()}: {self.quantity_change} {self.ingredient.unit} of {self.ingredient}"

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValueError("Stock movements cannot be changed.")
        super().save(*args, **kwargs)


class BatchSource(models.TextChoices):
    DELIVERY = "delivery", "Delivery"
    COUNT = "count", "Found in a stock count"
    OPENING = "opening", "Stock from before batch tracking"


class StockBatch(TimeStampedModel):
    """
    Stock of one ingredient that arrived together and shares an expiry date.

    A delivery creates a batch; so does a count that finds more stock than
    recorded (with no expiry date). Either way, the movement that created the
    batch (source_movement) has a line adding quantity_received to it, and
    every later change to the batch is another line. Opening batches hold the
    stock that existed before batch tracking began and have no source movement
    or creating line.

    An ingredient's quantity_on_hand is always the sum of its batches'
    quantity_remaining. Batches are kept after they run out.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    ingredient = models.ForeignKey(Ingredient, on_delete=models.PROTECT, related_name="batches")
    source = models.CharField(max_length=20, choices=BatchSource.choices)
    source_movement = models.ForeignKey(
        StockMovement,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="created_batches",
    )
    quantity_received = models.DecimalField(
        max_digits=QUANTITY_DIGITS, decimal_places=QUANTITY_PLACES
    )
    quantity_remaining = models.DecimalField(
        max_digits=QUANTITY_DIGITS, decimal_places=QUANTITY_PLACES
    )
    expires_on = models.DateField(null=True, blank=True)
    received_at = models.DateTimeField()

    class Meta:
        # Earliest expiry first; batches without an expiry date last, oldest first.
        ordering = [models.F("expires_on").asc(nulls_last=True), "received_at"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(quantity_received__gt=0),
                name="inventory_batch_received_positive",
            ),
            models.CheckConstraint(
                condition=models.Q(quantity_remaining__gte=0)
                & models.Q(quantity_remaining__lte=models.F("quantity_received")),
                name="inventory_batch_remaining_in_range",
            ),
            models.CheckConstraint(
                condition=models.Q(source__in=BatchSource.values), name="inventory_batch_source_valid"
            ),
            # Only opening batches lack the movement that created them.
            models.CheckConstraint(
                condition=models.Q(source=BatchSource.OPENING, source_movement__isnull=True)
                | (~models.Q(source=BatchSource.OPENING) & models.Q(source_movement__isnull=False)),
                name="inventory_batch_source_movement",
            ),
        ]

    def __str__(self):
        expiry = self.expires_on or "no expiry"
        return f"{self.ingredient}: {self.quantity_remaining} {self.ingredient.unit} ({expiry})"


class MovementLine(models.Model):
    """How much one movement added to (+) or took from (-) one batch."""

    movement = models.ForeignKey(StockMovement, on_delete=models.PROTECT, related_name="lines")
    batch = models.ForeignKey(StockBatch, on_delete=models.PROTECT, related_name="lines")
    quantity_change = models.DecimalField(
        max_digits=QUANTITY_DIGITS, decimal_places=QUANTITY_PLACES
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["movement", "batch"], name="inventory_line_unique_batch"),
            models.CheckConstraint(
                condition=~models.Q(quantity_change=0), name="inventory_line_change_not_zero"
            ),
        ]

    def __str__(self):
        return f"{self.quantity_change} to {self.batch}"

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValueError("Movement lines cannot be changed.")
        super().save(*args, **kwargs)
