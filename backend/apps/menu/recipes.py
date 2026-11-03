"""Turning portions of menu items into ingredient quantities, through their recipes."""

from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal

from apps.inventory.units import convert, round_quantity

from .models import RecipeLine


@dataclass
class RecipeTotals:
    # {ingredient_id: quantity in the ingredient's own unit}, only amounts that round above zero.
    by_ingredient: dict = field(default_factory=dict)
    # {menu_item_id: {ingredient_id, ...}} for items that have a recipe.
    ingredients_by_item: dict = field(default_factory=dict)


def ingredient_totals(organization, portions_by_item):
    """
    What `portions_by_item` ({menu_item_id: portions}) use of each ingredient.
    Amounts are converted exactly and rounded once per ingredient, not once
    per item. Recipes only ever count the organization's own ingredients.
    """
    exact = defaultdict(Decimal)
    by_item = defaultdict(set)
    recipes = RecipeLine.objects.filter(
        menu_item__in=list(portions_by_item), ingredient__organization=organization
    ).select_related("ingredient")
    for line in recipes:
        by_item[line.menu_item_id].add(line.ingredient_id)
        exact[line.ingredient_id] += convert(
            portions_by_item[line.menu_item_id] * line.quantity, line.unit, line.ingredient.unit
        )
    rounded = {pk: round_quantity(total) for pk, total in exact.items()}
    return RecipeTotals(
        by_ingredient={pk: total for pk, total in rounded.items() if total > 0},
        ingredients_by_item=dict(by_item),
    )
