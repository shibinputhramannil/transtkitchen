from datetime import date, timedelta

from django.db import IntegrityError, transaction
from django.db.models.functions import Lower
from django.shortcuts import get_object_or_404
from rest_framework import generics, status
from rest_framework.exceptions import APIException, ValidationError
from rest_framework.permissions import SAFE_METHODS
from rest_framework.response import Response

from apps.organizations.mixins import OrganizationScopedMixin
from apps.organizations.models import Role

from .models import MAX_QUANTITY, Ingredient, MovementKind, StockBatch, StockMovement
from .serializers import (
    DUPLICATE_NAME,
    CountSerializer,
    ExpiringBatchSerializer,
    ExpiryCorrectionSerializer,
    IngredientSerializer,
    ReceiveSerializer,
    StockBatchSerializer,
    StockMovementSerializer,
)
from .stock import record_stock_change

# Expiry dates further ahead than this are almost certainly typos.
MAX_EXPIRY_YEARS = 5
MAX_EXPIRING_WITHIN_DAYS = 30


class Conflict(APIException):
    status_code = status.HTTP_409_CONFLICT
    default_detail = "The request conflicts with the current state."
    default_code = "conflict"


def latest_expiry(today):
    try:
        return today.replace(year=today.year + MAX_EXPIRY_YEARS)
    except ValueError:  # 29 February
        return date(today.year + MAX_EXPIRY_YEARS, 2, 28)


def check_expiry_not_too_late(expires_on, today):
    if expires_on is not None and expires_on > latest_expiry(today):
        raise ValidationError(
            {"expires_on": [f"Expiry dates more than {MAX_EXPIRY_YEARS} years ahead are not accepted."]}
        )


class InventoryScopedMixin(OrganizationScopedMixin):
    """Puts the organization and its local date in every serializer's context."""

    def get_serializer_context(self):
        return {
            **super().get_serializer_context(),
            "organization": self.organization,
            "today": self.organization.local_today(),
        }


class IngredientScopedMixin(InventoryScopedMixin):
    """Ingredients of the requester's organization; staff read, managers edit."""

    def get_minimum_role(self):
        return Role.STAFF if self.request.method in SAFE_METHODS else Role.MANAGER

    def save_ingredient(self, serializer, **kwargs):
        try:
            with transaction.atomic():
                serializer.save(**kwargs)
        except IntegrityError:
            # Another request saved the same name in the meantime.
            raise ValidationError({"name": [DUPLICATE_NAME]}) from None


class IngredientListCreateView(IngredientScopedMixin, generics.ListCreateAPIView):
    """
    Lists active ingredients by name. ?search= matches part of the name;
    ?archived=true lists archived ingredients instead.
    """

    serializer_class = IngredientSerializer

    def get_queryset(self):
        archived = self.request.query_params.get("archived") == "true"
        queryset = (
            Ingredient.objects.filter(organization=self.organization, is_active=not archived)
            .with_expiry(self.organization.local_today())
            # Aggregating queries ignore Meta.ordering, so order explicitly.
            .order_by(Lower("name"))
        )
        search = self.request.query_params.get("search", "").strip()
        if search:
            queryset = queryset.filter(name__icontains=search)
        return queryset

    def perform_create(self, serializer):
        self.save_ingredient(serializer, organization=self.organization)


class IngredientDetailView(IngredientScopedMixin, generics.RetrieveUpdateDestroyAPIView):
    """
    The unit can change, and the ingredient can be deleted, only while it has no
    stock history. Changes lock the ingredient row, as stock movements do, so
    neither check can race with a movement being recorded.
    """

    serializer_class = IngredientSerializer
    http_method_names = ["get", "patch", "delete", "head", "options"]

    def get_queryset(self):
        queryset = Ingredient.objects.filter(organization=self.organization)
        if self.request.method in SAFE_METHODS:
            return queryset.with_expiry(self.organization.local_today())
        return queryset.select_for_update()

    @transaction.atomic
    def update(self, request, *args, **kwargs):
        return super().update(request, *args, **kwargs)

    @transaction.atomic
    def destroy(self, request, *args, **kwargs):
        return super().destroy(request, *args, **kwargs)

    def perform_update(self, serializer):
        ingredient = serializer.instance
        unit = serializer.validated_data.get("unit", ingredient.unit)
        if unit != ingredient.unit and ingredient.movements.exists():
            raise ValidationError(
                {"unit": ["The unit cannot change once the ingredient has stock history."]}
            )
        self.save_ingredient(serializer)

    def perform_destroy(self, instance):
        if instance.movements.exists():
            raise ValidationError(
                {"detail": "This ingredient has stock history and cannot be deleted. Archive it instead."}
            )
        instance.delete()


class StockChangeView(InventoryScopedMixin, generics.GenericAPIView):
    """
    Records one stock movement for an ingredient. Any member may record one.

    Each submission carries a client-chosen request_id. Sending the same
    request again returns the movement it already created (200) instead of
    changing stock twice, so retrying after a lost response is safe.
    """

    kind = None

    def get_queryset(self):
        return Ingredient.objects.filter(organization=self.organization)

    def get_change(self, ingredient, data):
        """Returns the signed change to apply, or raises."""
        raise NotImplementedError

    def is_same_request(self, movement, data):
        """Whether an earlier movement is what `data` would have recorded."""
        raise NotImplementedError

    def post(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        with transaction.atomic():
            # Locking the row makes concurrent stock changes, unit changes,
            # expiry corrections, and deletions of this ingredient run one at a time.
            ingredient = get_object_or_404(self.get_queryset().select_for_update(), pk=kwargs["pk"])

            previous = ingredient.movements.filter(request_id=data["request_id"]).first()
            if previous is not None:
                if previous.kind != self.kind or not self.is_same_request(previous, data):
                    raise Conflict("This request_id was already used for a different stock change.")
                return Response(self.movement_data(previous), status=status.HTTP_200_OK)

            if not ingredient.is_active:
                raise ValidationError({"detail": "Archived ingredients cannot change stock."})

            change = self.get_change(ingredient, data)
            if ingredient.quantity_on_hand + change > MAX_QUANTITY:
                raise ValidationError({"detail": f"Stock cannot exceed {MAX_QUANTITY}."})

            movement = record_stock_change(
                ingredient,
                kind=self.kind,
                change=change,
                request_id=data["request_id"],
                user=request.user,
                note=data["note"],
                expires_on=data.get("expires_on"),
            )

        return Response(self.movement_data(movement), status=status.HTTP_201_CREATED)

    def movement_data(self, movement):
        movement = StockMovement.objects.prefetch_related("lines__batch").get(pk=movement.pk)
        return StockMovementSerializer(movement).data


class ReceiveView(StockChangeView):
    """Adds a delivery to stock as a new batch, with an optional expiry date."""

    kind = MovementKind.RECEIVE
    serializer_class = ReceiveSerializer

    def get_change(self, ingredient, data):
        expires_on = data["expires_on"]
        today = self.organization.local_today()
        if expires_on is not None and expires_on < today:
            raise ValidationError({"expires_on": ["The expiry date is in the past."]})
        check_expiry_not_too_late(expires_on, today)
        return data["quantity"]

    def is_same_request(self, movement, data):
        return (
            movement.quantity_change == data["quantity"]
            and movement.expires_on == data["expires_on"]
        )


class CountView(StockChangeView):
    """
    Sets stock to a counted quantity and records the difference. A shortfall
    is taken from batches, earliest expiry first; extra stock becomes a new
    batch with no expiry date. With expected_quantity, the count is refused
    (409) if stock changed since the counter started, so a delivery recorded
    meanwhile is not silently undone.
    """

    kind = MovementKind.COUNT
    serializer_class = CountSerializer

    def get_change(self, ingredient, data):
        expected = data.get("expected_quantity")
        if expected is not None and expected != ingredient.quantity_on_hand:
            raise Conflict(
                f"Stock changed since the count began: it is now {ingredient.quantity_on_hand}. "
                "Check the count and submit it again."
            )
        return data["counted_quantity"] - ingredient.quantity_on_hand

    def is_same_request(self, movement, data):
        return movement.quantity_after == data["counted_quantity"]


class StockMovementListView(InventoryScopedMixin, generics.ListAPIView):
    """An ingredient's stock history, newest first. Movements cannot be changed."""

    serializer_class = StockMovementSerializer

    def get_queryset(self):
        ingredient = get_object_or_404(
            Ingredient.objects.filter(organization=self.organization), pk=self.kwargs["pk"]
        )
        return ingredient.movements.select_related("created_by").prefetch_related("lines__batch")


class StockBatchListView(InventoryScopedMixin, generics.ListAPIView):
    """
    An ingredient's batches that still hold stock, earliest expiry first.
    ?include_empty=true also lists batches that have run out.
    """

    serializer_class = StockBatchSerializer

    def get_queryset(self):
        ingredient = get_object_or_404(
            Ingredient.objects.filter(organization=self.organization), pk=self.kwargs["pk"]
        )
        batches = ingredient.batches.all()
        if self.request.query_params.get("include_empty") != "true":
            batches = batches.filter(quantity_remaining__gt=0)
        return batches


class StockBatchDetailView(InventoryScopedMixin, generics.GenericAPIView):
    """
    Corrects a batch's expiry date (managers and owners). The correction is
    recorded in the stock history; setting the same date again changes nothing.
    """

    serializer_class = ExpiryCorrectionSerializer
    minimum_role = Role.MANAGER
    http_method_names = ["patch", "options"]

    def patch(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        expires_on = serializer.validated_data["expires_on"]
        # Past dates are allowed: the printed date may show the stock already expired.
        check_expiry_not_too_late(expires_on, self.organization.local_today())

        with transaction.atomic():
            ingredient = get_object_or_404(
                Ingredient.objects.filter(organization=self.organization).select_for_update(),
                pk=kwargs["ingredient_pk"],
            )
            batch = get_object_or_404(ingredient.batches, pk=kwargs["pk"])
            if batch.expires_on != expires_on:
                note = f"Expiry date changed from {batch.expires_on or 'none'} to {expires_on or 'none'}."
                if serializer.validated_data["note"]:
                    note = f"{note} {serializer.validated_data['note']}"
                batch.expires_on = expires_on
                batch.save(update_fields=["expires_on", "updated_at"])
                StockMovement.objects.create(
                    ingredient=ingredient,
                    kind=MovementKind.EXPIRY_CORRECTION,
                    quantity_change=0,
                    quantity_after=ingredient.quantity_on_hand,
                    note=note,
                    created_by=request.user,
                    expires_on=expires_on,
                    corrected_batch=batch,
                )

        return Response(StockBatchSerializer(batch, context=self.get_serializer_context()).data)


class ExpiringBatchListView(InventoryScopedMixin, generics.ListAPIView):
    """
    Batches across the organization that still hold stock and are expired or
    expire within ?within= days (0 to 30, default 3), earliest first. "Today"
    is the organization's local date.
    """

    serializer_class = ExpiringBatchSerializer

    def get_queryset(self):
        raw = self.request.query_params.get("within", "3")
        try:
            within = int(raw)
        except ValueError:
            within = -1
        if not 0 <= within <= MAX_EXPIRING_WITHIN_DAYS:
            raise ValidationError(
                {"within": [f"Must be a whole number of days from 0 to {MAX_EXPIRING_WITHIN_DAYS}."]}
            )
        until = self.organization.local_today() + timedelta(days=within)
        return (
            StockBatch.objects.filter(
                ingredient__organization=self.organization,
                quantity_remaining__gt=0,
                expires_on__lte=until,
            )
            .select_related("ingredient")
            .order_by("expires_on", "received_at")
        )
