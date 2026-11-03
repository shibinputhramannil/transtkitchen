"""
Matching imported rows to menu items, previewing what an import will do, and
applying it. Applying is all-or-nothing: stock movements, batch deductions,
deduction records, and the applied status commit together or not at all.
"""

import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from django.db import transaction
from django.db.models import Count, Max, Sum
from django.utils import timezone

from apps.inventory.models import Ingredient, MovementKind, StockMovement
from apps.inventory.stock import record_stock_change
from apps.inventory.units import convert, round_quantity
from apps.menu.models import MenuItem, MenuItemAlias, RecipeLine
from apps.organizations.models import Organization

from .csv_import import start_of_day
from .models import ImportDeduction, ImportStatus, SaleLine, SalesImport

# Stock movements for an import use request IDs derived from the import and
# the ingredient, so the database itself refuses a second deduction.
DEDUCTION_NAMESPACE = uuid.UUID("4f1d3c52-7b1e-4f8a-9c55-0d7a2f6b9e10")

ACKNOWLEDGEABLE = ("row_errors", "shortfalls", "overlapping_dates", "counted_after_sales")
MAX_LISTED = 31


class ApplyRefused(Exception):
    def __init__(self, status, detail, preview=None, missing=()):
        super().__init__(detail)
        self.status = status
        self.detail = detail
        self.preview = preview
        self.missing = list(missing)


def usable_lines(sales_import):
    """Rows that will be applied: valid, not ignored."""
    return sales_import.lines.filter(error="", ignored=False)


def resolve_lines(sales_import, *, item_name=None, save=True):
    """
    Matches each usable row to a menu item: by a remembered item ID, then a
    remembered name, then a menu item with the same name (ignoring case).
    Limited to rows with `item_name` (ignoring case) when given.

    Returns the rows whose match changes. With save=False nothing is written,
    so a preview can report what re-matching would do. Ignored rows and rows
    with errors are never touched, so repeating this changes nothing further.
    """
    organization = sales_import.organization
    aliases = list(MenuItemAlias.objects.filter(organization=organization, source=sales_import.source))
    by_id = {alias.external_id: alias.menu_item_id for alias in aliases if alias.external_id}
    by_name = {alias.external_name.casefold(): alias.menu_item_id for alias in aliases}
    by_item = {
        name.casefold(): pk
        for pk, name in MenuItem.objects.filter(organization=organization).values_list("pk", "name")
    }
    lines = usable_lines(sales_import)
    if item_name is not None:
        lines = lines.filter(item_name__iexact=item_name)
    changed = []
    for line in lines:
        key = line.item_name.casefold()
        menu_item_id = (
            (by_id.get(line.external_id) if line.external_id else None)
            or by_name.get(key)
            or by_item.get(key)
        )
        if line.menu_item_id != menu_item_id:
            line.menu_item_id = menu_item_id
            changed.append(line)
    if save:
        SaleLine.objects.bulk_update(changed, ["menu_item"])
    return changed


@dataclass
class Requirements:
    """What the import's matched rows need, per ingredient (in its own unit)."""

    by_ingredient: dict = field(default_factory=dict)
    # The earliest moment any sale using the ingredient happened.
    earliest_sale: dict = field(default_factory=dict)
    item_totals: dict = field(default_factory=dict)
    items_without_recipe: set = field(default_factory=set)


def requirements_for(sales_import):
    zone = ZoneInfo(sales_import.organization.time_zone)
    item_totals = defaultdict(Decimal)
    item_earliest = {}
    rows = usable_lines(sales_import).filter(menu_item__isnull=False).values_list(
        "menu_item_id", "quantity", "sold_at", "business_date"
    )
    for menu_item_id, quantity, sold_at, business_date in rows:
        item_totals[menu_item_id] += quantity
        # Without a time, a sale may have happened at any point of its
        # business date, so the earliest it can have happened is the start
        # of that kitchen-local day.
        moment = sold_at or start_of_day(business_date, zone)
        if menu_item_id not in item_earliest or moment < item_earliest[menu_item_id]:
            item_earliest[menu_item_id] = moment

    result = Requirements(item_totals=dict(item_totals))
    exact = defaultdict(Decimal)
    recipes = RecipeLine.objects.filter(
        menu_item__in=item_totals,
        # Recipes are validated to use the organization's own ingredients; never trust otherwise.
        ingredient__organization=sales_import.organization,
    ).select_related("ingredient")
    with_recipe = set()
    for line in recipes:
        with_recipe.add(line.menu_item_id)
        exact[line.ingredient_id] += convert(
            item_totals[line.menu_item_id] * line.quantity, line.unit, line.ingredient.unit
        )
        earliest = item_earliest[line.menu_item_id]
        current = result.earliest_sale.get(line.ingredient_id)
        if current is None or earliest < current:
            result.earliest_sale[line.ingredient_id] = earliest
    # Round once per ingredient, not once per row.
    result.by_ingredient = {pk: round_quantity(total) for pk, total in exact.items() if round_quantity(total) > 0}
    result.items_without_recipe = set(item_totals) - with_recipe
    return result


def overlapping_dates(sales_import):
    dates = usable_lines(sales_import).values("business_date")
    return sorted(
        set(
            SaleLine.objects.filter(
                sales_import__organization=sales_import.organization,
                sales_import__status=ImportStatus.APPLIED,
                error="",
                ignored=False,
                business_date__in=dates,
            )
            .exclude(sales_import=sales_import)
            .values_list("business_date", flat=True)
        )
    )


def counted_after_sales(requirements):
    """
    Ingredients counted after the earliest sale that used them. A sale's moment
    is its sold_at time when the file gave one, otherwise the start of its
    business date in the organization's time zone.
    """
    if not requirements.earliest_sale:
        return []
    latest_counts = dict(
        StockMovement.objects.filter(
            ingredient__in=requirements.earliest_sale, kind=MovementKind.COUNT
        )
        .values("ingredient")
        .annotate(latest=Max("created_at"))
        .values_list("ingredient", "latest")
    )
    return [
        (ingredient_id, latest_counts[ingredient_id], earliest)
        for ingredient_id, earliest in requirements.earliest_sale.items()
        if ingredient_id in latest_counts and latest_counts[ingredient_id] > earliest
    ]


def text(quantity):
    """Quantities go out as strings with 3 decimal places, like every other endpoint."""
    return f"{quantity:.3f}"


def ingredient_data(ingredient):
    return {"id": str(ingredient.pk), "name": ingredient.name, "unit": ingredient.unit, "is_active": ingredient.is_active}


def build_preview(sales_import, ingredients=None, requirements=None):
    """
    Describes what applying the import would do, from current data. Pass
    locked `ingredients` ({pk: Ingredient}) and `requirements` while applying.
    """
    lines = sales_import.lines
    summary = {
        "rows": sales_import.row_count,
        "error_rows": lines.exclude(error="").count(),
        "ignored_rows": lines.filter(ignored=True).count(),
        "unmatched_rows": usable_lines(sales_import).filter(menu_item__isnull=True).count(),
        "matched_rows": usable_lines(sales_import).filter(menu_item__isnull=False).count(),
    }
    unmatched = [
        {"item_name": row["item_name"], "rows": row["rows"], "quantity": text(row["quantity"])}
        for row in usable_lines(sales_import)
        .filter(menu_item__isnull=True)
        .values("item_name")
        .annotate(rows=Count("id"), quantity=Sum("quantity"))
        .order_by("item_name")
    ]

    if sales_import.status == ImportStatus.APPLIED:
        deductions = [
            {
                "ingredient": ingredient_data(deduction.ingredient),
                "required": text(deduction.required),
                "taken": text(deduction.taken),
                "shortfall": text(deduction.shortfall),
            }
            for deduction in sales_import.deductions.select_related("ingredient").order_by("ingredient__name")
        ]
        items = item_rows(sales_import, requirements_for(sales_import))
        return {
            "summary": summary,
            "unmatched": unmatched,
            "items": items,
            "deductions": deductions,
            "warnings": [],
            "blocking": [],
            "can_apply": False,
            "rematch_available": 0,
        }

    requirements = requirements or requirements_for(sales_import)
    if ingredients is None:
        ingredients = Ingredient.objects.in_bulk(list(requirements.by_ingredient))
    deductions = []
    for ingredient_id, required in requirements.by_ingredient.items():
        ingredient = ingredients[ingredient_id]
        deductions.append(
            {
                "ingredient": ingredient_data(ingredient),
                "required": text(required),
                "on_hand": text(ingredient.quantity_on_hand),
                "shortfall": text(max(required - ingredient.quantity_on_hand, Decimal("0"))),
            }
        )
    deductions.sort(key=lambda item: item["ingredient"]["name"].casefold())

    warnings = []
    if summary["error_rows"]:
        warnings.append(
            {
                "code": "row_errors",
                "message": (
                    "1 row has errors and will be left out of this import. "
                    if summary["error_rows"] == 1
                    else f"{summary['error_rows']} rows have errors and will be left out of this import. "
                )
                + "Fix them in the file and upload it again to include them.",
            }
        )
    short = [item for item in deductions if Decimal(item["shortfall"]) > 0]
    if short:
        warnings.append(
            {
                "code": "shortfalls",
                "message": "Recorded stock is not enough for these sales. The sales will still be recorded, "
                "stock will be taken down to zero (never below), and the missing amount will be recorded "
                "as a shortfall. Count these ingredients afterwards.",
                "details": [
                    {"ingredient": item["ingredient"], "shortfall": item["shortfall"]} for item in short
                ],
            }
        )
    overlaps = overlapping_dates(sales_import)
    if overlaps:
        warnings.append(
            {
                "code": "overlapping_dates",
                "message": "Another applied import already has sales on these dates. If this file "
                "contains the same sales again, applying it counts them twice.",
                "details": [day.isoformat() for day in overlaps[:MAX_LISTED]],
            }
        )
    counted = counted_after_sales(requirements)
    if counted:
        warnings.append(
            {
                "code": "counted_after_sales",
                "message": "These ingredients were counted after some of these sales happened. A count "
                "records the stock physically there, so it may already include what these sales used, "
                "and deducting the sales as well could count that usage twice. Sales without a time are "
                "treated as happening at the start of their business date in the kitchen's time zone. "
                "Apply only if you are sure the count did not include these sales, or count again afterwards.",
                "details": [
                    {
                        "ingredient": ingredient_data(ingredients[ingredient_id]),
                        "counted_at": counted_at,
                        "earliest_sale": earliest,
                    }
                    for ingredient_id, counted_at, earliest in counted
                ],
            }
        )

    blocking = []
    if summary["unmatched_rows"]:
        blocking.append(
            {
                "code": "unmatched_items",
                "message": (
                    "1 item name is not matched to a menu item. "
                    if len(unmatched) == 1
                    else f"{len(unmatched)} item names are not matched to menu items. "
                )
                + "Map each one to a menu item, or ignore it.",
            }
        )
    archived = [item["ingredient"] for item in deductions if not item["ingredient"]["is_active"]]
    if archived:
        blocking.append(
            {
                "code": "archived_ingredients",
                "message": "Recipes use archived ingredients, which cannot change stock. Restore them "
                "or change the recipes.",
                "details": archived,
            }
        )
    if not summary["matched_rows"] and not summary["unmatched_rows"]:
        blocking.append({"code": "nothing_to_apply", "message": "No rows can be applied."})

    return {
        "summary": summary,
        "unmatched": unmatched,
        "items": item_rows(sales_import, requirements),
        "deductions": deductions,
        "warnings": warnings,
        "blocking": blocking,
        "can_apply": sales_import.status == ImportStatus.DRAFT and not blocking,
        # Rows that would match differently with the current menu and remembered
        # names. Worked out without writing; POST .../rematch/ applies it.
        "rematch_available": len(resolve_lines(sales_import, save=False))
        if sales_import.status == ImportStatus.DRAFT
        else 0,
    }


def item_rows(sales_import, requirements):
    names = MenuItem.objects.in_bulk(list(requirements.item_totals))
    rows = [
        {
            "menu_item": {"id": str(pk), "name": names[pk].name, "is_active": names[pk].is_active},
            "quantity": text(quantity),
            "has_recipe": pk not in requirements.items_without_recipe,
        }
        for pk, quantity in requirements.item_totals.items()
    ]
    return sorted(rows, key=lambda row: row["menu_item"]["name"].casefold())


def apply_import(organization, import_id, user, acknowledged):
    """
    Applies a draft import in one transaction. Returns (import, applied_now);
    applying an import that is already applied returns it unchanged.
    """
    with transaction.atomic():
        # One import at a time per organization, so overlap checks cannot race.
        Organization.objects.select_for_update().get(pk=organization.pk)
        sales_import = SalesImport.objects.select_for_update().get(pk=import_id, organization=organization)
        if sales_import.status == ImportStatus.APPLIED:
            return sales_import, False
        if sales_import.status == ImportStatus.DISCARDED:
            raise ApplyRefused(409, "This import was discarded.")

        # Applies the stored matches, exactly as the preview showed them; re-matching
        # is an explicit action of its own.
        requirements = requirements_for(sales_import)
        # Lock in a fixed order so two imports cannot deadlock each other.
        ingredients = {
            ingredient.pk: ingredient
            for ingredient in Ingredient.objects.select_for_update()
            .filter(organization=organization, pk__in=list(requirements.by_ingredient))
            .order_by("pk")
        }
        preview = build_preview(sales_import, ingredients=ingredients, requirements=requirements)
        if preview["blocking"]:
            raise ApplyRefused(400, "This import cannot be applied yet.", preview)
        missing = [w["code"] for w in preview["warnings"] if w["code"] not in acknowledged]
        if missing:
            raise ApplyRefused(409, "Acknowledge the warnings before applying.", preview, missing)

        note = f"Sales {sales_import.first_date} to {sales_import.last_date}: {sales_import.file_name}"[:255]
        for ingredient_id in sorted(ingredients):
            ingredient = ingredients[ingredient_id]
            required = requirements.by_ingredient[ingredient_id]
            taken = min(required, ingredient.quantity_on_hand)
            movement = None
            if taken > 0:
                movement = record_stock_change(
                    ingredient,
                    kind=MovementKind.SALE,
                    change=-taken,
                    request_id=uuid.uuid5(DEDUCTION_NAMESPACE, f"{sales_import.pk}:{ingredient_id}"),
                    user=user,
                    note=note,
                )
            ImportDeduction.objects.create(
                sales_import=sales_import,
                ingredient=ingredient,
                required=required,
                taken=taken,
                shortfall=required - taken,
                movement=movement,
            )

        sales_import.status = ImportStatus.APPLIED
        sales_import.applied_by = user
        sales_import.applied_at = timezone.now()
        sales_import.save(update_fields=["status", "applied_by", "applied_at", "updated_at"])
    return sales_import, True


def daily_sales(organization, start, end):
    return (
        SaleLine.objects.filter(
            sales_import__organization=organization,
            sales_import__status=ImportStatus.APPLIED,
            error="",
            ignored=False,
            menu_item__isnull=False,
            business_date__range=(start, end),
        )
        .values("business_date", "menu_item", "menu_item__name")
        .annotate(quantity=Sum("quantity"))
        .order_by("-business_date", "menu_item__name")
    )


def default_report_range(organization, days=7):
    end = organization.local_today()
    return end - timedelta(days=days - 1), end
