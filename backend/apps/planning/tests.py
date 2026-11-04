import threading
import uuid
from datetime import date, datetime, timedelta
from datetime import timezone as dt_timezone
from decimal import Decimal
from unittest import mock

from django.db import connection
from django.test import TestCase, TransactionTestCase
from django.urls import reverse
from rest_framework_simplejwt.tokens import RefreshToken

from apps.accounts.models import User
from apps.core.testing import HttpsAPIClient
from apps.inventory.models import Ingredient, MovementLine, StockBatch, StockMovement, Unit
from apps.inventory.tests import add_movement
from apps.menu.models import MenuItem, RecipeLine
from apps.organizations.models import Membership, Organization, Role
from apps.organizations.testing import PASSWORD, TenantTestCase, make_user, org_url
from apps.sales.models import ImportStatus, SaleLine, SalesImport
from apps.waste.models import WasteEntry

from .forecasting import forecast, suggestion
from .models import ForecastMethod, PlanStatus, PrepPlan, PrepPlanIngredient

# Tuesday 10 November 2026, 20:00 UTC. Tomorrow is Wednesday the 11th.
NOW = datetime(2026, 11, 10, 20, 0, tzinfo=dt_timezone.utc)
TODAY = date(2026, 11, 10)
TOMORROW = date(2026, 11, 11)


def at(moment):
    return mock.patch("django.utils.timezone.now", return_value=moment)


def wednesdays(count):
    """The `count` Wednesdays before TOMORROW, most recent first."""
    return [TOMORROW - timedelta(weeks=n) for n in range(1, count + 1)]


def record_sales(organization, day, quantities, status=ImportStatus.APPLIED, **line_fields):
    """One import of sales on `day`: {menu_item: quantity}."""
    sales_import = SalesImport.objects.create(
        organization=organization, source="csv", source_reference=uuid.uuid4().hex, status=status,
        first_date=day, last_date=day, row_count=len(quantities),
    )
    SaleLine.objects.bulk_create(
        SaleLine(
            sales_import=sales_import, row_number=index + 2, business_date=day, item_name=item.name,
            quantity=Decimal(str(quantity)), menu_item=item, **line_fields,
        )
        for index, (item, quantity) in enumerate(quantities.items())
    )


class PlanningTestCase(TenantTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        with at(NOW - timedelta(days=120)):
            cls.pizza = MenuItem.objects.create(organization=cls.org_a, name="Pizza")
            cls.soup = MenuItem.objects.create(organization=cls.org_a, name="Soup")
            cls.salad_b = MenuItem.objects.create(organization=cls.org_b, name="Salad")
        cls.flour = Ingredient.objects.create(organization=cls.org_a, name="Flour", unit=Unit.KILOGRAM)
        RecipeLine.objects.create(menu_item=cls.pizza, ingredient=cls.flour, quantity=250, unit="g")

    def setUp(self):
        super().setUp()
        clock = at(NOW)
        clock.start()
        self.addCleanup(clock.stop)

    def create_plan(self, plan_date=TOMORROW, user=None, **extra):
        self.authenticate(user or self.manager_a)
        return self.client.post(
            org_url("prep-plan-list", self.org_a), {"plan_date": plan_date.isoformat(), **extra}, format="json"
        )

    def plan_url(self, plan_id, name="prep-plan-detail"):
        return org_url(name, self.org_a, pk=plan_id)

    def line(self, body, item):
        return next(line for line in body["lines"] if line["menu_item"]["id"] == str(item.pk))


class ForecastTests(PlanningTestCase):
    def test_same_weekday_average(self):
        for day, quantity in zip(wednesdays(5), (10, 12, 14, 16, 100)):
            record_sales(self.org_a, day, {self.pizza: quantity})
        record_sales(self.org_a, TODAY, {self.pizza: 50})  # a Tuesday: not a Wednesday sample

        result = forecast(self.org_a, TOMORROW)[self.pizza.pk]

        self.assertEqual(result.method, ForecastMethod.WEEKDAY_AVERAGE)
        self.assertEqual(result.forecast, Decimal("13"))  # the last 4 Wednesdays only
        self.assertEqual([b["date"] for b in result.basis], [d.isoformat() for d in wednesdays(4)])

    def test_trading_days_without_the_item_count_as_zero(self):
        for day in wednesdays(4):
            record_sales(self.org_a, day, {self.soup: 5, self.pizza: 8} if day != wednesdays(1)[0] else {self.soup: 5})

        result = forecast(self.org_a, TOMORROW)[self.pizza.pk]

        self.assertEqual(result.forecast, Decimal("6"))  # (0 + 8 + 8 + 8) / 4
        self.assertEqual(result.basis[0], {"date": wednesdays(1)[0].isoformat(), "quantity": "0.000"})

    def test_days_with_no_imported_sales_are_unknown_not_zero(self):
        # The most recent Wednesday has no sales imported at all.
        for day in wednesdays(4)[1:]:
            record_sales(self.org_a, day, {self.pizza: 9})

        result = forecast(self.org_a, TOMORROW)[self.pizza.pk]

        self.assertEqual((result.method, result.forecast, len(result.basis)), (ForecastMethod.WEEKDAY_AVERAGE, Decimal("9"), 3))

    def test_falls_back_to_recent_trading_days(self):
        days = [TODAY - timedelta(days=n) for n in range(0, 16)]
        trading = [day for day in days if day.weekday() != TOMORROW.weekday()][:8]
        for day in trading:
            record_sales(self.org_a, day, {self.pizza: 4})
        record_sales(self.org_a, wednesdays(1)[0], {self.pizza: 10})

        result = forecast(self.org_a, TOMORROW)[self.pizza.pk]

        self.assertEqual(result.method, ForecastMethod.RECENT_AVERAGE)  # only one Wednesday
        self.assertEqual(len(result.basis), 9)
        self.assertEqual(result.forecast, Decimal("4.667"))

    def test_too_little_history_gives_no_forecast(self):
        for n in range(1, 6):
            record_sales(self.org_a, TODAY - timedelta(days=n), {self.pizza: 3})

        result = forecast(self.org_a, TOMORROW)[self.pizza.pk]

        self.assertEqual((result.method, result.forecast, result.basis), (ForecastMethod.NONE, None, []))
        self.assertEqual(suggestion(None, 10), 0)

    def test_new_items_ignore_days_before_they_existed(self):
        for day in wednesdays(6):
            record_sales(self.org_a, day, {self.soup: 5})
        with at(datetime(2026, 10, 25, 12, 0, tzinfo=dt_timezone.utc)):
            curry = MenuItem.objects.create(organization=self.org_a, name="Curry")
        for day in wednesdays(2):
            record_sales(self.org_a, day, {self.soup: 5, curry: 6})

        result = forecast(self.org_a, TOMORROW)[curry.pk]

        # Only the 2 Wednesdays since it was added count; without 3, and without 7 trading days, no forecast.
        self.assertEqual(result.method, ForecastMethod.NONE)

    def test_history_imported_after_creating_the_item_still_counts(self):
        with at(NOW):
            late = MenuItem.objects.create(organization=self.org_a, name="Late")
        for day in wednesdays(4):
            record_sales(self.org_a, day, {late: 2})

        self.assertEqual(forecast(self.org_a, TOMORROW)[late.pk].forecast, Decimal("2"))

    def test_only_applied_valid_rows_of_active_items_count(self):
        for day in wednesdays(4):
            record_sales(self.org_a, day, {self.pizza: 10})
            record_sales(self.org_a, day, {self.pizza: 99}, status=ImportStatus.DRAFT)
            record_sales(self.org_a, day, {self.pizza: 99}, ignored=True)
        archived = MenuItem.objects.create(organization=self.org_a, name="Old", is_active=False)

        results = forecast(self.org_a, TOMORROW)

        self.assertEqual(results[self.pizza.pk].forecast, Decimal("10"))
        self.assertNotIn(archived.pk, results)

    def test_other_organizations_sales_never_count(self):
        for day in wednesdays(4):
            record_sales(self.org_b, day, {self.salad_b: 50})

        results = forecast(self.org_a, TOMORROW)

        self.assertNotIn(self.salad_b.pk, results)
        self.assertEqual(results[self.pizza.pk].method, ForecastMethod.NONE)

    def test_suggestion_adds_the_buffer_and_rounds_up(self):
        self.assertEqual(suggestion(Decimal("13"), Decimal("10")), Decimal("15"))
        self.assertEqual(suggestion(Decimal("10"), Decimal("0")), Decimal("10"))
        self.assertEqual(suggestion(Decimal("0.1"), Decimal("0")), Decimal("1"))

    def test_forecast_endpoint(self):
        for day in wednesdays(4):
            record_sales(self.org_a, day, {self.pizza: 8})
        self.authenticate(self.staff_a)
        url = org_url("forecast", self.org_a)

        body = self.client.get(url).json()

        self.assertEqual(body["date"], "2026-11-11")
        pizza = next(r for r in body["results"] if r["menu_item"]["name"] == "Pizza")
        self.assertEqual((pizza["forecast"], pizza["method"]), ("8.000", "weekday_average"))
        self.assertEqual(self.client.get(url, {"date": "2026-11-09"}).status_code, 400)
        self.assertEqual(self.client.get(url, {"date": "2026-11-25"}).status_code, 400)
        self.authenticate(self.owner_b)
        self.assertEqual(self.client.get(url).status_code, 404)


class PlanTests(PlanningTestCase):
    def setUp(self):
        super().setUp()
        for day, quantity in zip(wednesdays(4), (10, 12, 14, 16)):
            record_sales(self.org_a, day, {self.pizza: quantity, self.soup: 3})

    def test_manager_creates_a_plan_from_the_forecast(self):
        response = self.create_plan()

        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertEqual((body["plan_date"], body["status"], body["version"], body["buffer_percent"]), ("2026-11-11", "draft", 1, "10.00"))
        pizza = self.line(body, self.pizza)
        # 13 forecast + 10% = 14.3, rounded up to 15.
        self.assertEqual((pizza["forecast"], pizza["suggested"], pizza["planned"], pizza["edited"]), ("13.000", "15.000", "15.000", False))
        self.assertEqual(len(pizza["basis"]), 4)

    def test_roles_and_tenants(self):
        self.assertEqual(self.create_plan(user=self.staff_a).status_code, 403)
        self.assertEqual(self.create_plan(user=self.owner_b).status_code, 404)
        plan = self.create_plan().json()["id"]
        self.authenticate(self.staff_a)
        self.assertEqual(self.client.get(self.plan_url(plan)).status_code, 200)
        self.assertEqual(self.client.patch(self.plan_url(plan), {"version": 1}, format="json").status_code, 403)
        self.authenticate(self.owner_b)
        self.assertEqual(self.client.get(self.plan_url(plan)).status_code, 404)
        self.assertEqual(self.client.get(org_url("prep-plan-detail", self.org_b, pk=plan)).status_code, 404)

    def test_plan_dates_are_today_to_two_weeks_ahead(self):
        cases = [(TODAY - timedelta(days=1), 400), (TODAY + timedelta(days=15), 400), (TODAY, 201), (TODAY + timedelta(days=14), 201)]
        for plan_date, expected in cases:
            with self.subTest(plan_date=plan_date):
                self.assertEqual(self.create_plan(plan_date).status_code, expected)

    def test_one_plan_per_date(self):
        first = self.create_plan().json()["id"]

        second = self.create_plan()

        self.assertEqual(second.status_code, 409)
        self.assertEqual(second.json()["plan"], first)

    def test_editing_planned_portions_notes_and_buffer(self):
        plan = self.create_plan().json()
        url = self.plan_url(plan["id"])

        edited = self.client.patch(url, {
            "version": 1, "lines": [{"menu_item": str(self.pizza.pk), "planned": "20", "note": "Football match"}],
        }, format="json").json()
        self.assertEqual(edited["version"], 2)
        pizza = self.line(edited, self.pizza)
        self.assertEqual((pizza["planned"], pizza["edited"], pizza["note"]), ("20.000", True, "Football match"))

        rebuffered = self.client.patch(url, {"version": 2, "buffer_percent": "50"}, format="json").json()
        # Suggestions follow the buffer; only lines nobody edited take them.
        self.assertEqual(self.line(rebuffered, self.pizza)["suggested"], "20.000")  # 13 × 1.5 = 19.5 → 20
        self.assertEqual(self.line(rebuffered, self.pizza)["planned"], "20.000")
        self.assertEqual(self.line(rebuffered, self.soup)["planned"], "5.000")  # 3 × 1.5 = 4.5 → 5

    def test_stale_versions_are_refused(self):
        plan = self.create_plan().json()["id"]
        url = self.plan_url(plan)
        self.client.patch(url, {"version": 1, "buffer_percent": "20"}, format="json")

        stale = self.client.patch(url, {"version": 1, "buffer_percent": "30"}, format="json")

        self.assertEqual(stale.status_code, 409)
        self.assertEqual(stale.json()["version"], 2)
        self.assertEqual(PrepPlan.objects.get().buffer_percent, Decimal("20"))

    def test_edit_validation(self):
        plan = self.create_plan().json()["id"]
        url = self.plan_url(plan)
        cases = [
            {"version": 1, "lines": [{"menu_item": str(self.salad_b.pk), "planned": "1"}]},
            {"version": 1, "lines": [{"menu_item": str(uuid.uuid4()), "planned": "1"}]},
            {"version": 1, "lines": [{"menu_item": str(self.pizza.pk), "planned": "-1"}]},
            {"version": 1, "lines": [{"menu_item": str(self.pizza.pk), "planned": "1.0001"}]},
            {"version": 1, "lines": [{"menu_item": str(self.pizza.pk)}, {"menu_item": str(self.pizza.pk)}]},
            {"version": 1, "buffer_percent": "201"},
            {"lines": []},
        ]
        for body in cases:
            with self.subTest(body=body):
                self.assertEqual(self.client.patch(url, body, format="json").status_code, 400)
        self.assertEqual(PrepPlan.objects.get().version, 1)

    def test_refresh_updates_untouched_lines_and_keeps_edits(self):
        plan = self.create_plan().json()["id"]
        url = self.plan_url(plan)
        self.client.patch(url, {"version": 1, "lines": [{"menu_item": str(self.pizza.pk), "planned": "30"}]}, format="json")
        # More soup sold on the latest Wednesday (3 + 27 = 30), and a new menu item.
        record_sales(self.org_a, wednesdays(1)[0], {self.soup: 27})
        burger = MenuItem.objects.create(organization=self.org_a, name="Burger")

        refreshed = self.client.post(self.plan_url(plan, "prep-plan-refresh"), {"version": 2}, format="json").json()

        self.assertEqual(refreshed["version"], 3)
        self.assertEqual(self.line(refreshed, self.pizza)["planned"], "30.000")  # edited: kept
        soup = self.line(refreshed, self.soup)
        # (30 + 3 + 3 + 3) / 4 = 9.75; with the 10% buffer, 10.725 rounds up to 11.
        self.assertEqual((soup["forecast"], soup["planned"]), ("9.750", "11.000"))
        self.assertIn(str(burger.pk), [line["menu_item"]["id"] for line in refreshed["lines"]])

    def test_viewing_a_plan_changes_nothing(self):
        plan = self.create_plan().json()
        record_sales(self.org_a, wednesdays(1)[0], {self.pizza: 100})
        self.authenticate(self.staff_a)

        body = self.client.get(self.plan_url(plan["id"])).json()

        self.assertEqual(body["version"], 1)
        self.assertEqual(self.line(body, self.pizza)["forecast"], "13.000")

    def test_delete_drafts_only(self):
        plan = self.create_plan().json()["id"]
        self.client.post(self.plan_url(plan, "prep-plan-finalize"), {"version": 1}, format="json")
        self.assertEqual(self.client.delete(self.plan_url(plan)).status_code, 409)
        self.client.post(self.plan_url(plan, "prep-plan-reopen"), {"version": 2}, format="json")
        self.authenticate(self.staff_a)
        self.assertEqual(self.client.delete(self.plan_url(plan)).status_code, 403)
        self.authenticate(self.manager_a)
        self.assertEqual(self.client.delete(self.plan_url(plan)).status_code, 204)
        self.assertFalse(PrepPlan.objects.exists())

    def test_list(self):
        self.create_plan(TOMORROW)
        self.create_plan(TODAY)
        self.authenticate(self.staff_a)

        results = self.client.get(org_url("prep-plan-list", self.org_a)).json()["results"]

        self.assertEqual([r["plan_date"] for r in results], ["2026-11-11", "2026-11-10"])


class IngredientNeedsTests(PlanningTestCase):
    def plan_with(self, pizzas):
        plan = self.create_plan().json()
        return self.client.patch(
            self.plan_url(plan["id"]),
            {"version": 1, "lines": [{"menu_item": str(self.pizza.pk), "planned": str(pizzas)}]},
            format="json",
        ).json()

    def test_converts_recipe_units_and_counts_only_usable_stock(self):
        add_movement(self.flour, "0.5", expires_on=TODAY)  # expired by tomorrow: not usable
        add_movement(self.flour, "0.4", expires_on=TOMORROW + timedelta(days=1))  # usable, use first
        add_movement(self.flour, "0.3", expires_on=TOMORROW + timedelta(days=20))

        current = self.plan_with(6)["current_ingredients"]

        self.assertIn("Live", current["note"])
        self.assertIsNotNone(current["as_of"])
        row = current["rows"][0]
        # 6 pizzas × 250 g = 1.5 kg.
        self.assertEqual(
            (row["unit"], row["required"], row["on_hand"], row["expiring_before"], row["usable"], row["shortfall"]),
            ("kg", "1.500", "1.200", "0.500", "0.700", "0.800"),
        )
        self.assertEqual([(b["quantity"], b["expires_on"]) for b in row["use_first"]], [("0.400", "2026-11-12")])

    def test_items_without_recipes_are_flagged(self):
        plan = self.create_plan().json()
        body = self.client.patch(
            self.plan_url(plan["id"]),
            {"version": 1, "lines": [{"menu_item": str(self.soup.pk), "planned": "4"}]},
            format="json",
        ).json()

        self.assertEqual(body["current_ingredients"]["items_without_recipe"], ["Soup"])

    def test_archived_ingredients_are_marked(self):
        Ingredient.objects.filter(pk=self.flour.pk).update(is_active=False)

        row = self.plan_with(2)["current_ingredients"]["rows"][0]

        self.assertIs(row["ingredient"]["is_active"], False)

    def test_insufficient_stock_is_a_shortfall_not_an_error(self):
        row = self.plan_with(8)["current_ingredients"]["rows"][0]

        self.assertEqual((row["required"], row["usable"], row["shortfall"]), ("2.000", "0.000", "2.000"))


class FinalizeTests(PlanningTestCase):
    def test_finalize_saves_a_labelled_snapshot_separate_from_live_stock(self):
        add_movement(self.flour, "1", expires_on=TOMORROW + timedelta(days=10))
        plan = self.create_plan().json()
        self.client.patch(
            self.plan_url(plan["id"]),
            {"version": 1, "lines": [{"menu_item": str(self.pizza.pk), "planned": "6"}]}, format="json",
        )

        final = self.client.post(self.plan_url(plan["id"], "prep-plan-finalize"), {"version": 2}, format="json").json()
        # Stock changes after finalizing.
        add_movement(self.flour, "5", expires_on=TOMORROW + timedelta(days=10))
        later = self.client.get(self.plan_url(plan["id"])).json()

        self.assertEqual((final["status"], final["version"]), ("final", 3))
        self.assertEqual(final["finalized_by"], "manager@a.example")
        snapshot = later["ingredient_snapshot"]
        self.assertEqual(snapshot["taken_at"], final["finalized_at"])
        self.assertIn("not current stock", snapshot["note"])
        self.assertEqual((snapshot["rows"][0]["usable"], snapshot["rows"][0]["shortfall"]), ("1.000", "0.500"))
        self.assertNotIn("use_first", snapshot["rows"][0])
        current = later["current_ingredients"]
        self.assertEqual((current["rows"][0]["usable"], current["rows"][0]["shortfall"]), ("6.000", "0.000"))
        self.assertEqual(PrepPlanIngredient.objects.count(), 1)

    def test_final_plans_refuse_edits_until_reopened(self):
        plan = self.create_plan().json()["id"]
        self.client.post(self.plan_url(plan, "prep-plan-finalize"), {"version": 1}, format="json")

        edit = self.client.patch(self.plan_url(plan), {"version": 2, "buffer_percent": "5"}, format="json")
        refresh = self.client.post(self.plan_url(plan, "prep-plan-refresh"), {"version": 2}, format="json")
        reopened = self.client.post(self.plan_url(plan, "prep-plan-reopen"), {"version": 2}, format="json")

        self.assertEqual((edit.status_code, refresh.status_code), (409, 409))
        self.assertEqual(reopened.status_code, 200)
        self.assertEqual((reopened.json()["status"], reopened.json()["ingredient_snapshot"]), ("draft", None))
        self.assertFalse(PrepPlanIngredient.objects.exists())

    def test_past_plans_cannot_be_reopened(self):
        plan = self.create_plan(TODAY).json()["id"]
        self.client.post(self.plan_url(plan, "prep-plan-finalize"), {"version": 1}, format="json")

        with at(NOW + timedelta(days=1)):
            response = self.client.post(self.plan_url(plan, "prep-plan-reopen"), {"version": 2}, format="json")

        self.assertEqual(response.status_code, 409)
        self.assertEqual(PrepPlan.objects.get().status, PlanStatus.FINAL)

    def test_staff_cannot_finalize_or_reopen(self):
        plan = self.create_plan().json()["id"]
        self.authenticate(self.staff_a)

        self.assertEqual(self.client.post(self.plan_url(plan, "prep-plan-finalize"), {"version": 1}, format="json").status_code, 403)


class ReviewTests(PlanningTestCase):
    def test_review_compares_plan_with_what_sold_and_was_overproduced(self):
        plan = self.create_plan(TODAY).json()["id"]
        later = NOW + timedelta(days=1)

        with at(later):
            self.authenticate(self.staff_a)
            before = self.client.get(self.plan_url(plan)).json()
            record_sales(self.org_a, TODAY, {self.pizza: 11})
            WasteEntry.objects.create(
                organization=self.org_a, menu_item=self.pizza, quantity=2, unit="each", reason="overproduction",
                wasted_on=TODAY, request_id=uuid.uuid4(),
            )
            after = self.client.get(self.plan_url(plan)).json()

        self.assertEqual(before["review"], {"sales_imported": False})
        self.assertEqual(self.line(before, self.pizza)["review"], {"sold": None, "overproduced": "0.000"})
        self.assertEqual(after["review"], {"sales_imported": True})
        self.assertEqual(self.line(after, self.pizza)["review"], {"sold": "11.000", "overproduced": "2.000"})
        self.assertEqual(self.line(after, self.soup)["review"]["sold"], "0.000")

    def test_upcoming_plans_have_no_review_but_show_recent_overproduction(self):
        for days_ago in (3, 10):
            WasteEntry.objects.create(
                organization=self.org_a, menu_item=self.pizza, quantity=4, unit="each", reason="overproduction",
                wasted_on=TODAY - timedelta(days=days_ago), request_id=uuid.uuid4(),
            )
        WasteEntry.objects.create(
            organization=self.org_a, menu_item=self.pizza, quantity=9, unit="each", reason="damaged",
            wasted_on=TODAY, request_id=uuid.uuid4(),
        )

        body = self.create_plan().json()

        self.assertIsNone(body["review"])
        self.assertEqual(self.line(body, self.pizza)["recent_overproduction"], {"portions": "8.000", "days": 2})
        self.assertIsNone(self.line(body, self.soup)["recent_overproduction"])


class NoStockChangeTests(PlanningTestCase):
    def test_planning_never_changes_stock(self):
        add_movement(self.flour, "3", expires_on=TOMORROW + timedelta(days=5))
        for day in wednesdays(4):
            record_sales(self.org_a, day, {self.pizza: 10})
        before = (
            StockMovement.objects.count(), MovementLine.objects.count(),
            list(StockBatch.objects.values_list("pk", "quantity_remaining")),
            Ingredient.objects.get(pk=self.flour.pk).quantity_on_hand,
        )

        plan = self.create_plan().json()["id"]
        self.client.patch(self.plan_url(plan), {"version": 1, "lines": [{"menu_item": str(self.pizza.pk), "planned": "40"}]}, format="json")
        self.client.post(self.plan_url(plan, "prep-plan-refresh"), {"version": 2}, format="json")
        self.client.post(self.plan_url(plan, "prep-plan-finalize"), {"version": 3}, format="json")
        self.client.post(self.plan_url(plan, "prep-plan-reopen"), {"version": 4}, format="json")
        self.client.get(org_url("forecast", self.org_a))
        self.client.delete(self.plan_url(plan))

        after = (
            StockMovement.objects.count(), MovementLine.objects.count(),
            list(StockBatch.objects.values_list("pk", "quantity_remaining")),
            Ingredient.objects.get(pk=self.flour.pk).quantity_on_hand,
        )
        self.assertEqual(after, before)


class AdminTests(TestCase):
    def test_plans_are_view_only(self):
        admin_user = User.objects.create_superuser(email="admin@example.com", password=PASSWORD)
        organization = Organization.objects.create(name="Bakery")
        plan = PrepPlan.objects.create(organization=organization, plan_date=TODAY)
        self.client.force_login(admin_user)
        change = reverse("admin:planning_prepplan_change", args=[plan.pk])

        self.assertEqual(self.client.get(change, secure=True).status_code, 200)
        self.assertEqual(self.client.post(change, {"status": "final"}, secure=True).status_code, 403)


class ConcurrentPlanTests(TransactionTestCase):
    """Parallel requests, each thread with its own PostgreSQL connection."""

    def setUp(self):
        self.assertEqual(connection.vendor, "postgresql")
        self.organization = Organization.objects.create(name="Bakery")
        self.manager = make_user("manager@example.com")
        self.owner = make_user("owner@example.com")
        Membership.objects.create(user=self.manager, organization=self.organization, role=Role.MANAGER)
        Membership.objects.create(user=self.owner, organization=self.organization, role=Role.OWNER)
        self.pizza = MenuItem.objects.create(organization=self.organization, name="Pizza")
        self.plan_date = (self.organization.local_today() + timedelta(days=1)).isoformat()

    def run_together(self, *requests):
        barrier = threading.Barrier(len(requests))
        results = [None] * len(requests)

        def send(index, user, method, url, data):
            client = HttpsAPIClient()
            client.credentials(HTTP_AUTHORIZATION=f"Bearer {RefreshToken.for_user(user).access_token}")
            try:
                barrier.wait(timeout=10)
                response = getattr(client, method)(url, data, format="json")
                results[index] = (response.status_code, response.json())
            finally:
                connection.close()

        threads = [threading.Thread(target=send, args=(i, *request)) for i, request in enumerate(requests)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        return results

    def test_two_creates_for_one_date_make_one_plan(self):
        url = org_url("prep-plan-list", self.organization)

        results = self.run_together(
            (self.manager, "post", url, {"plan_date": self.plan_date}),
            (self.owner, "post", url, {"plan_date": self.plan_date}),
        )

        self.assertEqual(sorted(status for status, _ in results), [201, 409])
        self.assertEqual(PrepPlan.objects.count(), 1)

    def test_two_edits_of_the_same_version_cannot_both_win(self):
        client = HttpsAPIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {RefreshToken.for_user(self.manager).access_token}")
        plan = client.post(org_url("prep-plan-list", self.organization), {"plan_date": self.plan_date}, format="json").json()
        url = org_url("prep-plan-detail", self.organization, pk=plan["id"])
        line = {"menu_item": str(self.pizza.pk)}

        results = self.run_together(
            (self.manager, "patch", url, {"version": 1, "lines": [{**line, "planned": "10"}]}),
            (self.owner, "patch", url, {"version": 1, "lines": [{**line, "planned": "20"}]}),
        )

        self.assertEqual(sorted(status for status, _ in results), [200, 409])
        winner = next(body for status, body in results if status == 200)
        saved = PrepPlan.objects.get()
        self.assertEqual(saved.version, 2)
        self.assertEqual(f"{saved.lines.get().planned:.3f}", winner["lines"][0]["planned"])
