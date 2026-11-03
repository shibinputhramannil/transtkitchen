"""
Logging and voiding waste. Each is one transaction: the entry, its stock
movements, batch lines, and deduction rows commit together or not at all.
"""

import uuid
from collections import defaultdict
from datetime import timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from django.db import IntegrityError, transaction
from django.db.models import Count, Max, Sum
from django.utils import timezone

from apps.inventory.models import MAX_QUANTITY, Ingredient, MovementKind, StockMovement
from apps.inventory.stock import record_stock_change, return_taken_stock
from apps.inventory.units import convert, round_quantity
from apps.menu.recipes import ingredient_totals
from apps.sales.csv_import import start_of_day

from .models import WasteDeduction, WasteEntry

# Stock movements of an entry use request IDs derived from the entry and the
# ingredient, so the database itself refuses a second deduction or reversal.
WASTE_NAMESPACE = uuid.UUID("9c0e6a3e-2d7b-4b8e-a4f1-6f3a5c2d8e71")
VOID_NAMESPACE = uuid.UUID("5b8d2f4a-1c6e-4a9b-8d3f-2e7c9a1b4f60")

MAX_DAYS_BACK = 7
ACKNOWLEDGEABLE = ("shortfall", "counted_after")


class WasteRefused(Exception):
    def __init__(self, status, detail, **data):
        super().__init__(detail)
        self.status = status
        self.detail = detail
        self.data = data


def text(quantity):
    return f"{quantity:.3f}"


def ingredient_data(ingredient):
    return {"id": str(ingredient.pk), "name": ingredient.name, "unit": ingredient.unit}


def same_request(entry, data):
    """Whether an earlier entry is what `data` would have recorded."""
    target = data.get("ingredient") or data.get("menu_item")
    return (
        (entry.ingredient_id or entry.menu_item_id) == target.pk
        and entry.batch_id == (data["batch"].pk if data.get("batch") else None)
        and entry.quantity == data["quantity"]
        and entry.unit == data["unit"]
        and entry.reason == data["reason"]
        and entry.note == data["note"]
        # Without an explicit date the entry took "today", which may have moved on since.
        and ("wasted_on" not in data or entry.wasted_on == data["wasted_on"])
    )


def wasted_moment(wasted_on, organization, now):
    """
    The earliest the waste can have happened. Waste logged for today is
    treated as happening now; waste logged for an earlier day may have
    happened at any time that day, so from the start of it in the kitchen's
    time zone.
    """
    if wasted_on >= organization.local_today():
        return now
    return start_of_day(wasted_on, ZoneInfo(organization.time_zone))


def log_waste(organization, user, data):
    """
    Records a waste entry and deducts its stock. Returns (entry, created_now);
    the same request_id with the same content returns the earlier entry.
    """
    with transaction.atomic():
        existing = WasteEntry.objects.filter(organization=organization, request_id=data["request_id"]).first()
        if existing is not None:
            return replay(existing, data)

        today = organization.local_today()
        wasted_on = data.get("wasted_on", today)
        if wasted_on > today:
            raise WasteRefused(400, "The waste date cannot be in the future.")
        if wasted_on < today - timedelta(days=MAX_DAYS_BACK):
            raise WasteRefused(400, f"Waste can be logged up to {MAX_DAYS_BACK} days back.")

        try:
            # Created first, so a simultaneous retry waits here on the unique
            # request_id instead of deducting stock a second time.
            with transaction.atomic():
                entry = WasteEntry.objects.create(
                    organization=organization,
                    ingredient=data.get("ingredient"),
                    menu_item=data.get("menu_item"),
                    batch=data.get("batch"),
                    quantity=data["quantity"],
                    unit=data["unit"],
                    reason=data["reason"],
                    wasted_on=wasted_on,
                    note=data["note"],
                    request_id=data["request_id"],
                    created_by=user,
                )
        except IntegrityError:
            existing = WasteEntry.objects.get(organization=organization, request_id=data["request_id"])
            return replay(existing, data)

        required = requirements(organization, data)
        # Lock in a fixed order so waste, imports, deliveries, and counts cannot deadlock.
        ingredients = {
            ingredient.pk: ingredient
            for ingredient in Ingredient.objects.select_for_update()
            .filter(organization=organization, pk__in=list(required))
            .order_by("pk")
        }
        check(entry, data, required, ingredients)

        for ingredient_id in sorted(ingredients):
            ingredient = ingredients[ingredient_id]
            amount = required[ingredient_id]
            taken = min(amount, ingredient.quantity_on_hand)
            movement = None
            if taken > 0:
                movement = record_stock_change(
                    ingredient,
                    kind=MovementKind.WASTE,
                    change=-taken,
                    request_id=uuid.uuid5(WASTE_NAMESPACE, f"{entry.pk}:{ingredient_id}"),
                    user=user,
                    note=f"Waste ({entry.get_reason_display()}) on {wasted_on}. {entry.note}".strip()[:255],
                    from_batch=entry.batch,
                )
            WasteDeduction.objects.create(
                entry=entry,
                ingredient=ingredient,
                unit=ingredient.unit,
                required=amount,
                taken=taken,
                shortfall=amount - taken,
                movement=movement,
            )
    return entry, True


def replay(existing, data):
    if not same_request(existing, data):
        raise WasteRefused(409, "This request_id was already used for a different waste entry.")
    return existing, False


def requirements(organization, data):
    """{ingredient_id: quantity in its own unit} that the entry wastes."""
    if data.get("ingredient"):
        ingredient = data["ingredient"]
        return {ingredient.pk: round_quantity(convert(data["quantity"], data["unit"], ingredient.unit))}
    return ingredient_totals(organization, {data["menu_item"].pk: data["quantity"]}).by_ingredient


def check(entry, data, required, ingredients):
    """Refuses the entry (rolling everything back) unless it can be applied as asked."""
    archived = [ingredient_data(i) for i in ingredients.values() if not i.is_active]
    if archived:
        raise WasteRefused(
            400, "Archived ingredients cannot change stock. Restore them first.", ingredients=archived
        )

    if entry.batch is not None:
        batch = entry.batch
        batch.refresh_from_db()
        amount = required[entry.ingredient_id]
        if batch.quantity_remaining < amount:
            raise WasteRefused(
                400,
                f"That batch holds only {text(batch.quantity_remaining)} {entry.ingredient.unit}.",
                batch_remaining=text(batch.quantity_remaining),
            )

    warnings = []
    short = [
        {"ingredient": ingredient_data(ingredients[pk]), "required": text(amount),
         "on_hand": text(ingredients[pk].quantity_on_hand),
         "shortfall": text(amount - ingredients[pk].quantity_on_hand)}
        for pk, amount in required.items()
        if amount > ingredients[pk].quantity_on_hand
    ]
    if short:
        warnings.append(
            {
                "code": "shortfall",
                "message": "Recorded stock is less than this waste. Stock will be taken down to zero "
                "(never below) and the rest recorded as a shortfall. Count these ingredients afterwards.",
                "details": short,
            }
        )
    moment = wasted_moment(entry.wasted_on, entry.organization, entry.created_at)
    counts = dict(
        StockMovement.objects.filter(ingredient__in=list(required), kind=MovementKind.COUNT)
        .values("ingredient")
        .annotate(latest=Max("created_at"))
        .values_list("ingredient", "latest")
    )
    counted = [
        {"ingredient": ingredient_data(ingredients[pk]), "counted_at": counts[pk]}
        for pk in required
        if pk in counts and counts[pk] > moment
    ]
    if counted:
        warnings.append(
            {
                "code": "counted_after",
                "message": "These ingredients were counted after this waste may have happened. A count "
                "records the stock physically there, so it may already leave out what was thrown away, and "
                "deducting the waste as well could count it twice. Waste for an earlier day is treated as "
                "happening at the start of that day in the kitchen's time zone.",
                "details": counted,
            }
        )
    missing = [w["code"] for w in warnings if w["code"] not in data["acknowledged"]]
    if missing:
        raise WasteRefused(
            409, "Acknowledge the warnings to log this waste.", missing_acknowledgements=missing, warnings=warnings
        )


def void_waste(organization, entry_id, user, reason):
    """
    Voids an entry and puts back exactly the stock it took, into the same
    batches. Returns (entry, voided_now); voiding again changes nothing.
    """
    with transaction.atomic():
        entry = WasteEntry.objects.select_for_update().get(pk=entry_id, organization=organization)
        if entry.voided_at is not None:
            return entry, False

        deductions = {d.ingredient_id: d for d in entry.deductions.select_related("movement")}
        ingredients = {
            ingredient.pk: ingredient
            for ingredient in Ingredient.objects.select_for_update()
            .filter(organization=organization, pk__in=list(deductions))
            .order_by("pk")
        }
        archived = [ingredient_data(i) for i in ingredients.values() if not i.is_active]
        if archived:
            raise WasteRefused(
                400, "Archived ingredients cannot change stock. Restore them first.", ingredients=archived
            )
        for ingredient_id in sorted(ingredients):
            deduction = deductions[ingredient_id]
            if deduction.movement is None:
                continue  # nothing was taken, so nothing goes back
            ingredient = ingredients[ingredient_id]
            if ingredient.quantity_on_hand + deduction.taken > MAX_QUANTITY:
                raise WasteRefused(400, f"Returning this stock would exceed {MAX_QUANTITY}.")
            deduction.reversal = return_taken_stock(
                ingredient,
                list(deduction.movement.lines.all()),
                kind=MovementKind.WASTE_VOID,
                request_id=uuid.uuid5(VOID_NAMESPACE, f"{entry.pk}:{ingredient_id}"),
                user=user,
                note=f"Waste entry voided: {reason}"[:255],
            )
            deduction.save(update_fields=["reversal"])

        entry.voided_at = timezone.now()
        entry.voided_by = user
        entry.void_reason = reason
        entry.save(update_fields=["voided_at", "voided_by", "void_reason", "updated_at"])
    return entry, True


def default_range(organization, days=7):
    end = organization.local_today()
    return end - timedelta(days=days - 1), end


def summary(organization, start, end):
    """
    Waste between two kitchen-local dates, leaving out voided entries.
    Quantities are only ever added up within one unit.
    """
    entries = WasteEntry.objects.filter(
        organization=organization, voided_at__isnull=True, wasted_on__range=(start, end)
    )
    deductions = WasteDeduction.objects.filter(entry__in=entries)

    by_ingredient = [
        {
            "ingredient": {"id": str(row["ingredient"]), "name": row["ingredient__name"]},
            "unit": row["unit"],
            "quantity": text(row["quantity"]),
            "entries": row["entries"],
        }
        for row in deductions.values("ingredient", "ingredient__name", "unit")
        .annotate(quantity=Sum("required"), entries=Count("entry", distinct=True))
        .order_by("ingredient__name", "unit")
    ]

    by_reason = defaultdict(lambda: {"entries": 0, "quantities": {}, "portions": Decimal("0")})
    for row in entries.values("reason").annotate(entries=Count("id")):
        by_reason[row["reason"]]["entries"] = row["entries"]
    for row in deductions.values("entry__reason", "unit").annotate(quantity=Sum("required")):
        by_reason[row["entry__reason"]]["quantities"][row["unit"]] = row["quantity"]
    for row in entries.filter(menu_item__isnull=False).values("reason").annotate(portions=Sum("quantity")):
        by_reason[row["reason"]]["portions"] = row["portions"]

    by_menu_item = [
        {
            "menu_item": {"id": str(row["menu_item"]), "name": row["menu_item__name"]},
            "portions": text(row["portions"]),
            "entries": row["entries"],
        }
        for row in entries.filter(menu_item__isnull=False)
        .values("menu_item", "menu_item__name")
        .annotate(portions=Sum("quantity"), entries=Count("id"))
        .order_by("menu_item__name")
    ]

    return {
        "from": start,
        "to": end,
        "entries": entries.count(),
        "by_ingredient": by_ingredient,
        "by_reason": [
            {
                "reason": reason,
                "entries": values["entries"],
                # Stock deducted, per stock unit; never added across units.
                "quantities": [
                    {"unit": unit, "quantity": text(quantity)}
                    for unit, quantity in sorted(values["quantities"].items())
                ],
                "portions": text(values["portions"]),
            }
            for reason, values in sorted(by_reason.items())
        ],
        "by_menu_item": by_menu_item,
    }
