import uuid
from datetime import date, timedelta

from django.shortcuts import get_object_or_404
from rest_framework import generics, status
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.organizations.mixins import OrganizationScopedMixin
from apps.organizations.models import Role

from .models import WasteEntry, WasteReason
from .serializers import VoidSerializer, WasteEntrySerializer, WasteLogSerializer
from .services import WasteRefused, default_range, log_waste, summary, void_waste

MAX_SUMMARY_DAYS = 92


def date_range(params, organization, max_days):
    start, end = default_range(organization)
    try:
        if "from" in params:
            start = date.fromisoformat(params["from"])
        if "to" in params:
            end = date.fromisoformat(params["to"])
    except ValueError:
        raise ValidationError({"detail": "Dates must be YYYY-MM-DD."}) from None
    if end < start or end - start >= timedelta(days=max_days):
        raise ValidationError({"detail": f"Choose a range of 1 to {max_days} days."})
    return start, end


def refusal_response(refusal):
    return Response({"detail": refusal.detail, **refusal.data}, status=refusal.status)


class WasteScopedMixin(OrganizationScopedMixin):
    """Any member logs and reads waste; voiding is for managers and owners."""

    def entries(self):
        return WasteEntry.objects.filter(organization=self.organization).select_related(
            "ingredient", "menu_item", "batch", "created_by", "voided_by"
        ).prefetch_related("deductions__ingredient")

    def entry_response(self, entry, status_code=status.HTTP_200_OK, **extra):
        entry = self.entries().get(pk=entry.pk)
        return Response({**WasteEntrySerializer(entry).data, **extra}, status=status_code)


class WasteListCreateView(WasteScopedMixin, generics.ListCreateAPIView):
    """
    Lists waste entries, newest first: ?from=&to= (kitchen-local dates),
    ?reason=, ?ingredient= (entries that took that ingredient), and
    ?include_voided=true. POST logs waste; sending the same request_id again
    returns the entry already logged.
    """

    serializer_class = WasteEntrySerializer
    minimum_role = Role.STAFF

    def get_queryset(self):
        params = self.request.query_params
        entries = self.entries()
        if params.get("include_voided") != "true":
            entries = entries.filter(voided_at__isnull=True)
        if "from" in params or "to" in params:
            start, end = date_range(params, self.organization, max_days=366)
            entries = entries.filter(wasted_on__range=(start, end))
        if params.get("reason"):
            if params["reason"] not in WasteReason.values:
                raise ValidationError({"reason": [f"Use one of: {', '.join(WasteReason.values)}."]})
            entries = entries.filter(reason=params["reason"])
        if params.get("ingredient"):
            try:
                ingredient_id = uuid.UUID(params["ingredient"])
            except ValueError:
                raise ValidationError({"ingredient": ["Must be an ingredient id."]}) from None
            entries = entries.filter(deductions__ingredient_id=ingredient_id).distinct()
        return entries

    def create(self, request, *args, **kwargs):
        serializer = WasteLogSerializer(data=request.data, context={"organization": self.organization})
        serializer.is_valid(raise_exception=True)
        try:
            entry, created = log_waste(self.organization, request.user, serializer.validated_data)
        except WasteRefused as refusal:
            return refusal_response(refusal)
        return self.entry_response(entry, status.HTTP_201_CREATED if created else status.HTTP_200_OK)


class WasteDetailView(WasteScopedMixin, APIView):
    def get(self, request, *args, **kwargs):
        return self.entry_response(get_object_or_404(self.entries(), pk=kwargs["pk"]))


class VoidWasteView(WasteScopedMixin, generics.GenericAPIView):
    """
    Voids an entry (managers and owners): the stock it took goes back to the
    same batches and the entry leaves the reports. Voiding again changes nothing.
    """

    serializer_class = VoidSerializer
    minimum_role = Role.MANAGER

    def post(self, request, *args, **kwargs):
        get_object_or_404(WasteEntry.objects.filter(organization=self.organization), pk=kwargs["pk"])
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            entry, voided_now = void_waste(
                self.organization, kwargs["pk"], request.user, serializer.validated_data["reason"]
            )
        except WasteRefused as refusal:
            return refusal_response(refusal)
        return self.entry_response(entry, voided_now=voided_now)


class WasteSummaryView(WasteScopedMixin, APIView):
    """
    Waste between ?from= and ?to= (kitchen-local dates; default the last 7
    days), leaving out voided entries. Quantities are grouped by unit and
    never added across units.
    """

    def get(self, request, *args, **kwargs):
        start, end = date_range(request.query_params, self.organization, MAX_SUMMARY_DAYS)
        return Response(summary(self.organization, start, end))
