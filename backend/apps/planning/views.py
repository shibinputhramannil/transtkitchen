from datetime import date, timedelta

from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import generics, status
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import SAFE_METHODS
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.organizations.mixins import OrganizationScopedMixin
from apps.organizations.models import Role

from . import services
from .forecasting import forecast
from .models import PlanStatus, PrepPlan
from .serializers import CreatePlanSerializer, PlanSummarySerializer, UpdatePlanSerializer, VersionSerializer
from .services import PlanRefused, text


def parse_date(value, name):
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        raise ValidationError({name: ["Use YYYY-MM-DD."]}) from None


def refusal_response(refusal):
    return Response({"detail": refusal.detail, **refusal.data}, status=refusal.status)


def ingredient_data(row):
    return {
        "ingredient": row["ingredient"],
        "unit": row["unit"],
        "required": text(row["required"]),
        "on_hand": text(row["on_hand"]),
        "expiring_before": text(row["expiring_before"]),
        "usable": text(row["usable"]),
        "shortfall": text(row["shortfall"]),
        **({"use_first": row["use_first"]} if "use_first" in row else {}),
    }


def plan_data(plan):
    """
    The plan, its lines, and its ingredient needs. `current_ingredients` is
    always calculated now from live stock and says so; `ingredient_snapshot`
    is what was saved when the plan was finalized (null for drafts). They are
    never mixed.
    """
    today = plan.organization.local_today()
    waste = services.overproduction(plan)
    live = services.ingredient_rows(plan)
    review = services.review(plan) if plan.plan_date < today else None
    lines = []
    for line in plan.lines.select_related("menu_item"):
        portions, days = waste.get(line.menu_item_id, (None, 0))
        lines.append(
            {
                "menu_item": {"id": str(line.menu_item_id), "name": line.menu_item.name, "is_active": line.menu_item.is_active},
                "forecast": text(line.forecast) if line.forecast is not None else None,
                "method": line.method,
                "basis": line.basis,
                "suggested": text(line.suggested),
                "planned": text(line.planned),
                "edited": line.edited,
                "note": line.note,
                "recent_overproduction": {"portions": text(portions), "days": days} if portions else None,
                "review": review["items"].get(line.menu_item_id) if review else None,
            }
        )
    snapshot = None
    if plan.status == PlanStatus.FINAL:
        snapshot = {
            "taken_at": plan.finalized_at,
            "note": "Saved when the plan was finalized. These are the figures at that time, not current stock.",
            "rows": [
                ingredient_data(
                    {
                        "ingredient": {"id": str(row.ingredient_id), "name": row.ingredient.name, "is_active": row.ingredient.is_active},
                        "unit": row.unit, "required": row.required, "on_hand": row.on_hand,
                        "expiring_before": row.expiring_before, "usable": row.usable, "shortfall": row.shortfall,
                    }
                )
                for row in plan.ingredient_snapshot.select_related("ingredient")
            ],
        }
    return {
        **PlanSummarySerializer(plan).data,
        "lines": lines,
        "current_ingredients": {
            "as_of": timezone.now(),
            "note": "Live: today's stock and expiry, applied to the planned portions as they are now.",
            "rows": [ingredient_data(row) for row in live["rows"]],
            "items_without_recipe": live["items_without_recipe"],
        },
        "ingredient_snapshot": snapshot,
        "review": {"sales_imported": review["sales_imported"]} if review else None,
    }


class PlanningScopedMixin(OrganizationScopedMixin):
    """Staff see forecasts and plans; managers and owners make and change plans."""

    def get_minimum_role(self):
        return Role.STAFF if self.request.method in SAFE_METHODS else Role.MANAGER

    def plans(self):
        return PrepPlan.objects.filter(organization=self.organization)

    def plan_response(self, plan_id, status_code=status.HTTP_200_OK):
        plan = self.plans().select_related("organization", "created_by", "finalized_by").get(pk=plan_id)
        return Response(plan_data(plan), status=status_code)


class ForecastView(PlanningScopedMixin, APIView):
    """The forecast for ?date= (default tomorrow, kitchen time), without making a plan."""

    def get(self, request, *args, **kwargs):
        today = self.organization.local_today()
        target = parse_date(request.query_params["date"], "date") if "date" in request.query_params else today + timedelta(days=1)
        if not today <= target <= today + timedelta(days=services.MAX_DAYS_AHEAD):
            raise ValidationError({"date": [f"Choose today up to {services.MAX_DAYS_AHEAD} days ahead."]})
        items = sorted(forecast(self.organization, target).values(), key=lambda f: f.menu_item.name.casefold())
        return Response(
            {
                "date": target,
                "results": [
                    {
                        "menu_item": {"id": str(f.menu_item.pk), "name": f.menu_item.name},
                        "forecast": text(f.forecast) if f.forecast is not None else None,
                        "method": f.method,
                        "basis": f.basis,
                    }
                    for f in items
                ],
            }
        )


class PlanListCreateView(PlanningScopedMixin, generics.GenericAPIView):
    """
    Lists plans between ?from= and ?to= (default two weeks either side of
    today), newest date first. POST creates the plan for a date.
    """

    def get(self, request, *args, **kwargs):
        today = self.organization.local_today()
        start = parse_date(request.query_params["from"], "from") if "from" in request.query_params else today - timedelta(days=14)
        end = parse_date(request.query_params["to"], "to") if "to" in request.query_params else today + timedelta(days=14)
        if end < start or end - start > timedelta(days=366):
            raise ValidationError({"detail": "Choose a range of up to a year."})
        plans = self.plans().filter(plan_date__range=(start, end)).select_related("created_by", "finalized_by")
        return Response({"results": PlanSummarySerializer(plans, many=True).data})

    def post(self, request, *args, **kwargs):
        serializer = CreatePlanSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            plan = services.create_plan(
                self.organization, request.user, serializer.validated_data["plan_date"],
                serializer.validated_data["buffer_percent"],
            )
        except PlanRefused as refusal:
            return refusal_response(refusal)
        return self.plan_response(plan.pk, status.HTTP_201_CREATED)


class PlanDetailView(PlanningScopedMixin, APIView):
    """
    GET: the plan, live ingredient needs, the finalized snapshot if any, and a
    review once the date has passed. PATCH (drafts): {version, buffer_percent?,
    lines: [{menu_item, planned?, note?}]}. DELETE: drafts only.
    """

    def get(self, request, *args, **kwargs):
        get_object_or_404(self.plans(), pk=kwargs["pk"])
        return self.plan_response(kwargs["pk"])

    def patch(self, request, *args, **kwargs):
        get_object_or_404(self.plans(), pk=kwargs["pk"])
        serializer = UpdatePlanSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        try:
            services.update_plan(
                self.organization, kwargs["pk"], request.user, data["version"],
                buffer_percent=data.get("buffer_percent"), lines=data["lines"],
            )
        except PlanRefused as refusal:
            return refusal_response(refusal)
        return self.plan_response(kwargs["pk"])

    def delete(self, request, *args, **kwargs):
        get_object_or_404(self.plans(), pk=kwargs["pk"])
        try:
            services.delete_plan(self.organization, kwargs["pk"])
        except PlanRefused as refusal:
            return refusal_response(refusal)
        return Response(status=status.HTTP_204_NO_CONTENT)


class PlanActionView(PlanningScopedMixin, APIView):
    """POST {version}: refresh (re-forecast drafts), finalize, or reopen."""

    action = None
    ACTIONS = {
        "refresh": services.refresh_plan,
        "finalize": services.finalize_plan,
        "reopen": services.reopen_plan,
    }

    def post(self, request, *args, **kwargs):
        get_object_or_404(self.plans(), pk=kwargs["pk"])
        serializer = VersionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            self.ACTIONS[self.action](self.organization, kwargs["pk"], request.user, serializer.validated_data["version"])
        except PlanRefused as refusal:
            return refusal_response(refusal)
        return self.plan_response(kwargs["pk"])
