from django.db import IntegrityError, transaction
from django.shortcuts import get_object_or_404
from rest_framework import generics, status
from rest_framework.exceptions import APIException, ValidationError
from rest_framework.permissions import SAFE_METHODS
from rest_framework.response import Response

from apps.organizations.mixins import OrganizationScopedMixin
from apps.organizations.models import Role

from .models import MAX_QUANTITY, Ingredient, MovementKind, StockMovement
from .serializers import (
    DUPLICATE_NAME,
    CountSerializer,
    IngredientSerializer,
    ReceiveSerializer,
    StockMovementSerializer,
)


class Conflict(APIException):
    status_code = status.HTTP_409_CONFLICT
    default_detail = "The request conflicts with the current state."
    default_code = "conflict"


class IngredientScopedMixin(OrganizationScopedMixin):
    """Ingredients of the requester's organization; staff read, managers edit."""

    def get_minimum_role(self):
        return Role.STAFF if self.request.method in SAFE_METHODS else Role.MANAGER

    def get_serializer_context(self):
        return {**super().get_serializer_context(), "organization": self.organization}

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
        queryset = Ingredient.objects.filter(organization=self.organization, is_active=not archived)
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
        if self.request.method not in SAFE_METHODS:
            queryset = queryset.select_for_update()
        return queryset

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


class StockChangeView(OrganizationScopedMixin, generics.GenericAPIView):
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
            # Locking the row makes concurrent stock changes, unit changes, and
            # deletions of this ingredient run one at a time.
            ingredient = get_object_or_404(self.get_queryset().select_for_update(), pk=kwargs["pk"])

            previous = ingredient.movements.filter(request_id=data["request_id"]).first()
            if previous is not None:
                if previous.kind != self.kind or not self.is_same_request(previous, data):
                    raise Conflict("This request_id was already used for a different stock change.")
                return Response(StockMovementSerializer(previous).data, status=status.HTTP_200_OK)

            if not ingredient.is_active:
                raise ValidationError({"detail": "Archived ingredients cannot change stock."})

            change = self.get_change(ingredient, data)
            quantity_after = ingredient.quantity_on_hand + change
            if quantity_after > MAX_QUANTITY:
                raise ValidationError({"detail": f"Stock cannot exceed {MAX_QUANTITY}."})

            ingredient.quantity_on_hand = quantity_after
            ingredient.save(update_fields=["quantity_on_hand", "updated_at"])
            movement = StockMovement.objects.create(
                ingredient=ingredient,
                kind=self.kind,
                quantity_change=change,
                quantity_after=quantity_after,
                note=data["note"],
                created_by=request.user,
                request_id=data["request_id"],
            )

        return Response(StockMovementSerializer(movement).data, status=status.HTTP_201_CREATED)


class ReceiveView(StockChangeView):
    """Adds a delivery to stock."""

    kind = MovementKind.RECEIVE
    serializer_class = ReceiveSerializer

    def get_change(self, ingredient, data):
        return data["quantity"]

    def is_same_request(self, movement, data):
        return movement.quantity_change == data["quantity"]


class CountView(StockChangeView):
    """
    Sets stock to a counted quantity and records the difference. With
    expected_quantity, the count is refused (409) if stock changed since the
    counter started, so a delivery recorded meanwhile is not silently undone.
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


class StockMovementListView(OrganizationScopedMixin, generics.ListAPIView):
    """An ingredient's stock history, newest first. Movements cannot be changed."""

    serializer_class = StockMovementSerializer

    def get_queryset(self):
        ingredient = get_object_or_404(
            Ingredient.objects.filter(organization=self.organization), pk=self.kwargs["pk"]
        )
        return ingredient.movements.select_related("created_by")
