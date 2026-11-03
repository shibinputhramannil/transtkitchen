"""
Unit conversions. Quantities convert only within one kind of measure:
grams and kilograms, millilitres and litres, and counted items.
"""

from decimal import ROUND_HALF_UP, Decimal

from .models import QUANTITY_PLACES

# unit: (kind of measure, size in that kind's smallest unit)
UNITS = {
    "g": ("mass", 1),
    "kg": ("mass", 1000),
    "ml": ("volume", 1),
    "l": ("volume", 1000),
    "each": ("count", 1),
}

QUANTUM = Decimal(1).scaleb(-QUANTITY_PLACES)


def measure(unit):
    return UNITS[unit][0]


def compatible(unit, other):
    return measure(unit) == measure(other)


def convert(quantity, from_unit, to_unit):
    """Converts exactly (no rounding); the units must be compatible."""
    if not compatible(from_unit, to_unit):
        raise ValueError(f"Cannot convert {from_unit} to {to_unit}.")
    return quantity * UNITS[from_unit][1] / UNITS[to_unit][1]


def round_quantity(quantity):
    """Rounds half up to the 3 decimal places stock quantities use."""
    return quantity.quantize(QUANTUM, rounding=ROUND_HALF_UP)
