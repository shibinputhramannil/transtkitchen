"""
Prep plans: creating them from forecasts, editing, refreshing, finalizing,
reopening, and working out what their planned portions need. Nothing here
changes stock.
"""

from datetime import timedelta
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.db.models import Count, Sum
from django.utils import timezone

from apps.inventory.models import Ingredient, StockBatch
from apps.inventory.stock import expiry_status
from apps.menu.models import MenuItem
from apps.menu.recipes import ingredient_totals
from apps.waste.models import WasteEntry, WasteReason

from .forecasting import applied_sales, forecast, suggestion
from .models import PlanStatus, PrepPlan, PrepPlanIngredient, PrepPlanLine

MAX_DAYS_AHEAD = 14
WASTE_CONTEXT_DAYS = 28


class PlanRefused(Exception):
    def __init__(self, status, detail, **data):
        super().__init__(detail)
        self.status = status
        self.detail = detail
        self.data = data


def text(quantity):
    return f"{quantity:.3f}"


def check_plan_date(organization, plan_date):
    today = organization.local_today()
    if not today <= plan_date <= today + timedelta(days=MAX_DAYS_AHEAD):
        raise PlanRefused(400, f"Plans can be made for today up to {MAX_DAYS_AHEAD} days ahead (kitchen time).")


def line_from_forecast(plan, item_forecast):
    suggested = suggestion(item_forecast.forecast, plan.buffer_percent)
    return PrepPlanLine(
        plan=plan,
        menu_item=item_forecast.menu_item,
        forecast=item_forecast.forecast,
        method=item_forecast.method,
        basis=item_forecast.basis,
        suggested=suggested,
        planned=suggested,
    )


def create_plan(organization, user, plan_date, buffer_percent):
    check_plan_date(organization, plan_date)
    try:
        with transaction.atomic():
            plan = PrepPlan.objects.create(
                organization=organization,
                plan_date=plan_date,
                buffer_percent=buffer_percent,
                created_by=user,
                updated_by=user,
            )
            PrepPlanLine.objects.bulk_create(
                line_from_forecast(plan, item) for item in forecast(organization, plan_date).values()
            )
    except IntegrityError:
        existing = PrepPlan.objects.get(organization=organization, plan_date=plan_date)
        raise PlanRefused(409, "A plan already exists for this date.", plan=str(existing.pk)) from None
    return plan


def locked_plan(organization, plan_id, version, *, status=None):
    """The plan row, locked, after checking the caller saw its current version."""
    plan = PrepPlan.objects.select_for_update().get(pk=plan_id, organization=organization)
    if plan.version != version:
        raise PlanRefused(409, "Someone else changed this plan. Reload it and try again.", version=plan.version)
    if status is not None and plan.status != status:
        raise PlanRefused(
            409, "Finalized plans cannot be changed. Reopen it first." if status == PlanStatus.DRAFT
            else "This plan is not finalized."
        )
    return plan


def bump(plan, user, *fields):
    plan.version += 1
    plan.updated_by = user
    plan.save(update_fields=["version", "updated_by", "updated_at", *fields])


def update_plan(organization, plan_id, user, version, buffer_percent=None, lines=()):
    with transaction.atomic():
        plan = locked_plan(organization, plan_id, version, status=PlanStatus.DRAFT)
        existing = {line.menu_item_id: line for line in plan.lines.all()}
        unknown = [str(change["menu_item"]) for change in lines if change["menu_item"] not in existing]
        if unknown:
            raise PlanRefused(400, "These menu items are not in this plan.", menu_items=unknown)

        changed = set()
        if buffer_percent is not None and buffer_percent != plan.buffer_percent:
            plan.buffer_percent = buffer_percent
            for line in existing.values():
                line.suggested = suggestion(line.forecast, buffer_percent)
                if not line.edited:
                    line.planned = line.suggested
                changed.add(line.menu_item_id)
        for change in lines:
            line = existing[change["menu_item"]]
            if "planned" in change:
                line.planned = change["planned"]
                line.edited = True
            if "note" in change:
                line.note = change["note"]
            changed.add(line.menu_item_id)
        PrepPlanLine.objects.bulk_update(
            [existing[pk] for pk in changed], ["suggested", "planned", "edited", "note"]
        )
        bump(plan, user, "buffer_percent")
    return plan


def refresh_plan(organization, plan_id, user, version):
    """
    Recalculates the forecasts. Lines nobody edited take the new suggestion;
    edited lines keep their planned portions. Newly active menu items are added.
    """
    with transaction.atomic():
        plan = locked_plan(organization, plan_id, version, status=PlanStatus.DRAFT)
        existing = {line.menu_item_id: line for line in plan.lines.select_related("menu_item")}
        items = list(MenuItem.objects.filter(organization=organization, is_active=True)) + [
            line.menu_item for line in existing.values() if not line.menu_item.is_active
        ]
        forecasts = forecast(organization, plan.plan_date, items)
        updated, added = [], []
        for menu_item_id, item_forecast in forecasts.items():
            line = existing.get(menu_item_id)
            if line is None:
                added.append(line_from_forecast(plan, item_forecast))
                continue
            line.forecast = item_forecast.forecast
            line.method = item_forecast.method
            line.basis = item_forecast.basis
            line.suggested = suggestion(item_forecast.forecast, plan.buffer_percent)
            if not line.edited:
                line.planned = line.suggested
            updated.append(line)
        PrepPlanLine.objects.bulk_update(updated, ["forecast", "method", "basis", "suggested", "planned"])
        PrepPlanLine.objects.bulk_create(added)
        bump(plan, user)
    return plan


def finalize_plan(organization, plan_id, user, version):
    """Locks the plan and saves what it needed, with the stock figures of this moment."""
    with transaction.atomic():
        plan = locked_plan(organization, plan_id, version, status=PlanStatus.DRAFT)
        rows = ingredient_rows(plan)["rows"]
        plan.ingredient_snapshot.all().delete()
        PrepPlanIngredient.objects.bulk_create(
            PrepPlanIngredient(
                plan=plan,
                ingredient_id=row["ingredient_id"],
                unit=row["unit"],
                required=row["required"],
                on_hand=row["on_hand"],
                expiring_before=row["expiring_before"],
                usable=row["usable"],
                shortfall=row["shortfall"],
            )
            for row in rows
        )
        plan.status = PlanStatus.FINAL
        plan.finalized_at = timezone.now()
        plan.finalized_by = user
        bump(plan, user, "status", "finalized_at", "finalized_by")
    return plan


def reopen_plan(organization, plan_id, user, version):
    """Makes a final plan editable again, until its date has passed. The snapshot is discarded."""
    with transaction.atomic():
        plan = locked_plan(organization, plan_id, version, status=PlanStatus.FINAL)
        if plan.plan_date < organization.local_today():
            raise PlanRefused(409, "Plans for past dates cannot be reopened.")
        plan.ingredient_snapshot.all().delete()
        plan.status = PlanStatus.DRAFT
        plan.finalized_at = None
        plan.finalized_by = None
        bump(plan, user, "status", "finalized_at", "finalized_by")
    return plan


def delete_plan(organization, plan_id):
    with transaction.atomic():
        plan = PrepPlan.objects.select_for_update().get(pk=plan_id, organization=organization)
        if plan.status != PlanStatus.DRAFT:
            raise PlanRefused(409, "Only draft plans can be deleted.")
        plan.delete()


def ingredient_rows(plan):
    """
    What the plan's planned portions need of each ingredient, against stock
    as it is right now. Stock expiring before the plan date is not usable on
    it; batches that will be close to expiry on the plan date are listed to
    use first. This is always calculated live.
    """
    lines = list(plan.lines.select_related("menu_item"))
    portions = {line.menu_item_id: line.planned for line in lines if line.planned > 0}
    totals = ingredient_totals(plan.organization, portions)
    ingredients = Ingredient.objects.filter(organization=plan.organization, pk__in=list(totals.by_ingredient))
    batches_by_ingredient = {}
    for batch in StockBatch.objects.filter(ingredient__in=list(totals.by_ingredient), quantity_remaining__gt=0):
        batches_by_ingredient.setdefault(batch.ingredient_id, []).append(batch)

    rows = []
    for ingredient in ingredients:
        required = totals.by_ingredient[ingredient.pk]
        batches = batches_by_ingredient.get(ingredient.pk, [])
        expiring_before = sum(
            (b.quantity_remaining for b in batches if b.expires_on is not None and b.expires_on < plan.plan_date),
            Decimal("0"),
        )
        usable = ingredient.quantity_on_hand - expiring_before
        rows.append(
            {
                "ingredient_id": ingredient.pk,
                "ingredient": {"id": str(ingredient.pk), "name": ingredient.name, "is_active": ingredient.is_active},
                "unit": ingredient.unit,
                "required": required,
                "on_hand": ingredient.quantity_on_hand,
                "expiring_before": expiring_before,
                "usable": usable,
                "shortfall": max(required - usable, Decimal("0")),
                "use_first": [
                    {"batch": str(b.pk), "quantity": text(b.quantity_remaining), "expires_on": b.expires_on.isoformat()}
                    for b in batches
                    if b.expires_on is not None and expiry_status(b.expires_on, plan.plan_date) == "expiring"
                ],
            }
        )
    rows.sort(key=lambda row: row["ingredient"]["name"].casefold())
    without_recipe = sorted(
        line.menu_item.name for line in lines if line.planned > 0 and line.menu_item_id not in totals.ingredients_by_item
    )
    return {"rows": rows, "items_without_recipe": without_recipe}


def overproduction(plan):
    """Menu-item overproduction waste in the 4 weeks before the plan date: {menu_item_id: (portions, days)}."""
    start = plan.plan_date - timedelta(days=WASTE_CONTEXT_DAYS)
    rows = (
        WasteEntry.objects.filter(
            organization=plan.organization,
            menu_item__isnull=False,
            reason=WasteReason.OVERPRODUCTION,
            voided_at__isnull=True,
            wasted_on__gte=start,
            wasted_on__lt=plan.plan_date,
        )
        .values("menu_item")
        .annotate(portions=Sum("quantity"), days=Count("wasted_on", distinct=True))
    )
    return {row["menu_item"]: (row["portions"], row["days"]) for row in rows}


def review(plan):
    """For a past plan: what actually sold and was overproduced that day, per menu item."""
    sales = applied_sales(plan.organization).filter(business_date=plan.plan_date)
    imported = sales.exists()
    sold = dict(sales.values("menu_item").annotate(total=Sum("quantity")).values_list("menu_item", "total"))
    wasted = dict(
        WasteEntry.objects.filter(
            organization=plan.organization,
            menu_item__isnull=False,
            reason=WasteReason.OVERPRODUCTION,
            voided_at__isnull=True,
            wasted_on=plan.plan_date,
        )
        .values("menu_item")
        .annotate(portions=Sum("quantity"))
        .values_list("menu_item", "portions")
    )
    return {
        "sales_imported": imported,
        "items": {
            menu_item_id: {
                "sold": text(sold.get(menu_item_id, Decimal("0"))) if imported else None,
                "overproduced": text(wasted.get(menu_item_id, Decimal("0"))),
            }
            for menu_item_id in plan.lines.values_list("menu_item", flat=True)
        },
    }
