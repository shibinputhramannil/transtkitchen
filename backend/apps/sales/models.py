import uuid

from django.conf import settings
from django.db import models

from apps.core.models import TimeStampedModel
from apps.inventory.models import QUANTITY_DIGITS, QUANTITY_PLACES, Ingredient, StockMovement
from apps.menu.models import MenuItem, SaleSource
from apps.organizations.models import Organization


def quantity_field(**kwargs):
    return models.DecimalField(max_digits=QUANTITY_DIGITS, decimal_places=QUANTITY_PLACES, **kwargs)


class ImportStatus(models.TextChoices):
    DRAFT = "draft", "Draft"
    APPLIED = "applied", "Applied"
    DISCARDED = "discarded", "Discarded"


class SalesImport(TimeStampedModel):
    """
    One batch of sales from a source: a CSV file now, a point-of-sale sync
    later. A draft can be previewed and corrected; applying it records the
    sales and deducts their ingredients from stock, once.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name="sales_imports"
    )
    source = models.CharField(max_length=20, choices=SaleSource.choices)
    # Identifies the data within its source: a CSV file's SHA-256 hash, or a
    # point-of-sale batch ID. The same data cannot be imported twice.
    source_reference = models.CharField(max_length=128)
    file_name = models.CharField(max_length=255, blank=True)
    status = models.CharField(max_length=20, choices=ImportStatus.choices, default=ImportStatus.DRAFT)
    row_count = models.PositiveIntegerField(default=0)
    first_date = models.DateField(null=True, blank=True)
    last_date = models.DateField(null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+"
    )
    applied_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    applied_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "source", "source_reference"],
                condition=~models.Q(status=ImportStatus.DISCARDED),
                name="sales_import_unique_reference",
            ),
            models.CheckConstraint(
                condition=models.Q(status__in=ImportStatus.values), name="sales_import_status_valid"
            ),
            models.CheckConstraint(
                condition=models.Q(source__in=SaleSource.values), name="sales_import_source_valid"
            ),
        ]

    def __str__(self):
        return f"{self.file_name or self.source_reference} ({self.status})"


class SaleLine(models.Model):
    """
    One row of an import: a quantity of an item sold on a business date (the
    kitchen-local date). Rows that failed validation keep their error and are
    never applied. Only rows of applied imports count as sales.
    """

    sales_import = models.ForeignKey(SalesImport, on_delete=models.CASCADE, related_name="lines")
    row_number = models.PositiveIntegerField()
    business_date = models.DateField(null=True, blank=True)
    sold_at = models.DateTimeField(null=True, blank=True)
    item_name = models.CharField(max_length=200, blank=True)
    external_id = models.CharField(max_length=100, blank=True)
    quantity = quantity_field(null=True, blank=True)
    menu_item = models.ForeignKey(
        MenuItem, on_delete=models.PROTECT, null=True, blank=True, related_name="sale_lines"
    )
    # Excluded from this import on purpose (an item the kitchen does not track).
    ignored = models.BooleanField(default=False)
    error = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ["row_number"]
        indexes = [models.Index(fields=["business_date", "menu_item"], name="sales_line_date_item")]
        constraints = [
            models.UniqueConstraint(fields=["sales_import", "row_number"], name="sales_line_unique_row"),
            # A valid row has a date, a name, and a positive quantity.
            models.CheckConstraint(
                condition=~models.Q(error="")
                | (
                    models.Q(business_date__isnull=False, quantity__gt=0)
                    & ~models.Q(item_name="")
                ),
                name="sales_line_valid_or_error",
            ),
        ]

    def __str__(self):
        return f"Row {self.row_number}: {self.quantity} × {self.item_name}"


class ImportDeduction(models.Model):
    """
    What an applied import needed of one ingredient, what it took from stock,
    and what was missing (stock never goes below zero). `movement` is the
    stock change, or empty when there was nothing to take.
    """

    sales_import = models.ForeignKey(SalesImport, on_delete=models.CASCADE, related_name="deductions")
    ingredient = models.ForeignKey(Ingredient, on_delete=models.PROTECT, related_name="+")
    required = quantity_field()
    taken = quantity_field()
    shortfall = quantity_field()
    movement = models.OneToOneField(
        StockMovement, on_delete=models.PROTECT, null=True, blank=True, related_name="sales_deduction"
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["sales_import", "ingredient"], name="sales_deduction_unique_ingredient"
            ),
            models.CheckConstraint(
                condition=models.Q(taken__gte=0, shortfall__gte=0)
                & models.Q(required=models.F("taken") + models.F("shortfall")),
                name="sales_deduction_adds_up",
            ),
            models.CheckConstraint(
                condition=models.Q(taken=0, movement__isnull=True)
                | models.Q(taken__gt=0, movement__isnull=False),
                name="sales_deduction_movement",
            ),
        ]
