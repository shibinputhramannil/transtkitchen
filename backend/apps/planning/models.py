import uuid

from django.conf import settings
from django.db import models

from apps.core.models import TimeStampedModel
from apps.inventory.models import QUANTITY_DIGITS, QUANTITY_PLACES, Ingredient, Unit
from apps.menu.models import MenuItem
from apps.organizations.models import Organization


def quantity_field(**kwargs):
    return models.DecimalField(max_digits=QUANTITY_DIGITS, decimal_places=QUANTITY_PLACES, **kwargs)


class PlanStatus(models.TextChoices):
    DRAFT = "draft", "Draft"
    FINAL = "final", "Final"


class ForecastMethod(models.TextChoices):
    WEEKDAY_AVERAGE = "weekday_average", "Average of recent same weekdays"
    RECENT_AVERAGE = "recent_average", "Average of recent trading days"
    NONE = "none", "Not enough history"


class PrepPlan(TimeStampedModel):
    """
    What the kitchen plans to prepare for one business date: portions per menu
    item, starting from a forecast plus a buffer and edited by managers.
    Plans never change stock. `version` goes up with every change, so two
    people editing at once cannot overwrite each other unknowingly.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="prep_plans")
    plan_date = models.DateField()
    status = models.CharField(max_length=10, choices=PlanStatus.choices, default=PlanStatus.DRAFT)
    # Added on top of the forecast for suggested portions, e.g. 10.00 for 10%.
    buffer_percent = models.DecimalField(max_digits=5, decimal_places=2, default=10)
    version = models.PositiveIntegerField(default=1)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+")
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    finalized_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    finalized_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-plan_date"]
        constraints = [
            models.UniqueConstraint(fields=["organization", "plan_date"], name="planning_plan_unique_date"),
            models.CheckConstraint(condition=models.Q(status__in=PlanStatus.values), name="planning_plan_status_valid"),
            models.CheckConstraint(
                condition=models.Q(buffer_percent__gte=0, buffer_percent__lte=200), name="planning_plan_buffer_range"
            ),
            models.CheckConstraint(condition=models.Q(version__gte=1), name="planning_plan_version_positive"),
            # A final plan records when it was finalized; a draft does not.
            models.CheckConstraint(
                condition=models.Q(status=PlanStatus.FINAL, finalized_at__isnull=False)
                | models.Q(status=PlanStatus.DRAFT, finalized_at__isnull=True),
                name="planning_plan_finalized_fields",
            ),
        ]

    def __str__(self):
        return f"Prep plan for {self.plan_date} ({self.status})"


class PrepPlanLine(models.Model):
    """
    One menu item in a plan: the forecast taken when the plan was created or
    refreshed (with the sales it was based on), the suggestion (forecast plus
    buffer, rounded up to whole portions), and the planned portions. `edited`
    means a person set the planned portions; refreshing never overwrites them.
    """

    plan = models.ForeignKey(PrepPlan, on_delete=models.CASCADE, related_name="lines")
    menu_item = models.ForeignKey(MenuItem, on_delete=models.PROTECT, related_name="prep_plan_lines")
    forecast = quantity_field(null=True, blank=True)
    method = models.CharField(max_length=20, choices=ForecastMethod.choices)
    # [{"date": "YYYY-MM-DD", "quantity": "12.000"}, ...]: the days the forecast averaged.
    basis = models.JSONField(default=list, blank=True)
    suggested = quantity_field()
    planned = quantity_field()
    edited = models.BooleanField(default=False)
    note = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ["menu_item__name"]
        constraints = [
            models.UniqueConstraint(fields=["plan", "menu_item"], name="planning_line_unique_item"),
            models.CheckConstraint(
                condition=models.Q(suggested__gte=0, planned__gte=0), name="planning_line_not_negative"
            ),
            models.CheckConstraint(condition=models.Q(method__in=ForecastMethod.values), name="planning_line_method_valid"),
            # There is a forecast exactly when there was enough history.
            models.CheckConstraint(
                condition=models.Q(method=ForecastMethod.NONE, forecast__isnull=True)
                | (~models.Q(method=ForecastMethod.NONE) & models.Q(forecast__gte=0)),
                name="planning_line_forecast_method",
            ),
        ]

    def __str__(self):
        return f"{self.planned} × {self.menu_item} on {self.plan.plan_date}"


class PrepPlanIngredient(models.Model):
    """
    A saved snapshot, written when a plan is finalized: what the planned
    portions needed of one ingredient and the stock figures at that moment.
    It is history, not current stock.
    """

    plan = models.ForeignKey(PrepPlan, on_delete=models.CASCADE, related_name="ingredient_snapshot")
    ingredient = models.ForeignKey(Ingredient, on_delete=models.PROTECT, related_name="prep_plan_snapshots")
    unit = models.CharField(max_length=10, choices=Unit.choices)
    required = quantity_field()
    on_hand = quantity_field()
    # Stock that would have expired before the plan date, so not counted as usable.
    expiring_before = quantity_field()
    usable = quantity_field()
    shortfall = quantity_field()

    class Meta:
        ordering = ["ingredient__name"]
        constraints = [
            models.UniqueConstraint(fields=["plan", "ingredient"], name="planning_snapshot_unique_ingredient"),
            models.CheckConstraint(
                condition=models.Q(required__gte=0, on_hand__gte=0, expiring_before__gte=0, usable__gte=0, shortfall__gte=0),
                name="planning_snapshot_not_negative",
            ),
            models.CheckConstraint(condition=models.Q(unit__in=Unit.values), name="planning_snapshot_unit_valid"),
        ]
