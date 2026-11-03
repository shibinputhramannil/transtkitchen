"""
Changing stock. Every function here expects the caller to hold the
ingredient's row lock (select_for_update) inside a transaction, so batches,
lines, movements, and quantity_on_hand change together and one at a time.
"""

from decimal import Decimal

from django.db.models import Sum

from .models import BatchSource, MovementKind, MovementLine, StockBatch, StockMovement

# A batch is "expiring" from this many days before its expiry date.
EXPIRING_SOON_DAYS = 3


class InsufficientStock(Exception):
    pass


def record_stock_change(ingredient, *, kind, change, request_id, user=None, note="", expires_on=None):
    """
    Applies `change` to the ingredient and records it as a movement with lines.
    An increase becomes a new batch (a delivery, or stock found by a count);
    a decrease is taken from existing batches, earliest expiry first.
    """
    quantity_after = ingredient.quantity_on_hand + change
    ingredient.quantity_on_hand = quantity_after
    ingredient.save(update_fields=["quantity_on_hand", "updated_at"])
    movement = StockMovement.objects.create(
        ingredient=ingredient,
        kind=kind,
        quantity_change=change,
        quantity_after=quantity_after,
        note=note,
        created_by=user,
        request_id=request_id,
        expires_on=expires_on,
    )
    if change > 0:
        batch = StockBatch.objects.create(
            ingredient=ingredient,
            source=BatchSource.DELIVERY if kind == MovementKind.RECEIVE else BatchSource.COUNT,
            source_movement=movement,
            quantity_received=change,
            quantity_remaining=change,
            expires_on=expires_on,
            received_at=movement.created_at,
        )
        MovementLine.objects.create(movement=movement, batch=batch, quantity_change=change)
    elif change < 0:
        take_from_batches(ingredient, movement, -change)
    return movement


def take_from_batches(ingredient, movement, quantity):
    """
    Takes `quantity` from the ingredient's batches, earliest expiry first
    (batches without an expiry date last, oldest first), recording one line
    per batch on `movement`.
    """
    lines = []
    for batch in ingredient.batches.filter(quantity_remaining__gt=0):
        if quantity == 0:
            break
        taken = min(batch.quantity_remaining, quantity)
        batch.quantity_remaining -= taken
        batch.save(update_fields=["quantity_remaining", "updated_at"])
        lines.append(MovementLine(movement=movement, batch=batch, quantity_change=-taken))
        quantity -= taken
    if quantity > 0:
        raise InsufficientStock(f"{ingredient} is short by {quantity} {ingredient.unit}.")
    MovementLine.objects.bulk_create(lines)
    return lines


def expiry_status(expires_on, today):
    if expires_on is None:
        return "no_expiry"
    days_left = (expires_on - today).days
    if days_left < 0:
        return "expired"
    if days_left <= EXPIRING_SOON_DAYS:
        return "expiring"
    return "ok"


def stock_problems(ingredient):
    """
    Lists every way the ingredient's stock, batches, movements, and lines
    disagree. An empty list means they are consistent.
    """
    problems = []
    batches = list(ingredient.batches.all())

    in_batches = sum((batch.quantity_remaining for batch in batches), Decimal("0"))
    if in_batches != ingredient.quantity_on_hand:
        problems.append(
            f"quantity_on_hand is {ingredient.quantity_on_hand} but batches hold {in_batches}"
        )

    for batch in batches:
        lines = batch.lines.aggregate(total=Sum("quantity_change"))["total"] or Decimal("0")
        start = batch.quantity_received if batch.source == BatchSource.OPENING else Decimal("0")
        if start + lines != batch.quantity_remaining:
            problems.append(
                f"batch {batch.pk} holds {batch.quantity_remaining} but its lines give {start + lines}"
            )
        if batch.source != BatchSource.OPENING and not batch.lines.filter(
            movement=batch.source_movement, quantity_change=batch.quantity_received
        ).exists():
            problems.append(f"batch {batch.pk} has no line from the movement that created it")

    for movement in ingredient.movements.all():
        total = movement.lines.aggregate(total=Sum("quantity_change"))["total"]
        if movement.tracks_batches and (total or Decimal("0")) != movement.quantity_change:
            problems.append(
                f"movement {movement.pk} changed stock by {movement.quantity_change} "
                f"but its lines add up to {total or 0}"
            )
        if not movement.tracks_batches and total is not None:
            problems.append(f"movement {movement.pk} predates batch tracking but has lines")

    return problems
