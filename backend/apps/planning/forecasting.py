"""
Forecasting portions sold per menu item for one business date. Read-only.

A trading day is a business date with any applied sales in the organization.
On a trading day, an item with no sales sold 0; a date with no applied sales
at all is unknown, not zero, so a missed import never drags forecasts down.
An item's history starts at its first appearance (the earlier of its first
sale and the day it was created), so days before it existed do not count as
zero sales either.

Methods, in order:
1. weekday_average: the average of the last 4 trading days on the target's
   weekday (within 8 weeks), when there are at least 3.
2. recent_average: the average of the last 14 trading days, when there are
   at least 7.
3. none: not enough history; the manager decides.
"""

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import ROUND_CEILING, Decimal
from zoneinfo import ZoneInfo

from django.db.models import Min, Sum

from apps.inventory.units import round_quantity
from apps.menu.models import MenuItem
from apps.sales.models import ImportStatus, SaleLine

from .models import ForecastMethod

LOOKBACK_DAYS = 56
WEEKDAY_SAMPLES = 4
WEEKDAY_MINIMUM = 3
RECENT_SAMPLES = 14
RECENT_MINIMUM = 7


@dataclass
class ItemForecast:
    menu_item: MenuItem
    forecast: Decimal | None
    method: str
    basis: list = field(default_factory=list)


def applied_sales(organization):
    """Rows that count as sales: from applied imports, valid, not ignored, matched."""
    return SaleLine.objects.filter(
        sales_import__organization=organization,
        sales_import__status=ImportStatus.APPLIED,
        error="",
        ignored=False,
        menu_item__isnull=False,
    )


def suggestion(forecast, buffer_percent):
    """The forecast plus the buffer, rounded up to whole portions."""
    if forecast is None:
        return Decimal("0")
    with_buffer = forecast * (Decimal("1") + Decimal(buffer_percent) / Decimal("100"))
    return with_buffer.to_integral_value(rounding=ROUND_CEILING)


def forecast(organization, target_date, menu_items=None):
    """
    {menu_item_id: ItemForecast} for the organization's active menu items (or
    `menu_items`). History is the 8 weeks before `target_date` and never
    includes the target date itself or anything after the kitchen's today.
    """
    today = organization.local_today()
    end = min(target_date - timedelta(days=1), today)
    start = target_date - timedelta(days=LOOKBACK_DAYS)
    if menu_items is None:
        menu_items = MenuItem.objects.filter(organization=organization, is_active=True)
    menu_items = list(menu_items)

    sales = applied_sales(organization)
    in_window = sales.filter(business_date__range=(start, end))
    trading_days = sorted(set(in_window.values_list("business_date", flat=True)), reverse=True)
    sold = defaultdict(Decimal)
    for menu_item_id, business_date, quantity in (
        in_window.values("menu_item", "business_date").annotate(total=Sum("quantity")).values_list(
            "menu_item", "business_date", "total"
        )
    ):
        sold[(menu_item_id, business_date)] = quantity
    first_sales = dict(
        sales.filter(menu_item__in=menu_items).values("menu_item").annotate(first=Min("business_date"))
        .values_list("menu_item", "first")
    )
    zone = ZoneInfo(organization.time_zone)

    results = {}
    for item in menu_items:
        created_on = item.created_at.astimezone(zone).date()
        first_seen = min(created_on, first_sales.get(item.pk, created_on))
        days = [day for day in trading_days if day >= first_seen]
        same_weekday = [day for day in days if day.weekday() == target_date.weekday()][:WEEKDAY_SAMPLES]
        if len(same_weekday) >= WEEKDAY_MINIMUM:
            method, chosen = ForecastMethod.WEEKDAY_AVERAGE, same_weekday
        elif len(days) >= RECENT_MINIMUM:
            method, chosen = ForecastMethod.RECENT_AVERAGE, days[:RECENT_SAMPLES]
        else:
            results[item.pk] = ItemForecast(item, None, ForecastMethod.NONE, [])
            continue
        quantities = [sold.get((item.pk, day), Decimal("0")) for day in chosen]
        average = round_quantity(sum(quantities, Decimal("0")) / len(quantities))
        basis = [{"date": day.isoformat(), "quantity": f"{quantity:.3f}"} for day, quantity in zip(chosen, quantities)]
        results[item.pk] = ItemForecast(item, average, method, basis)
    return results
