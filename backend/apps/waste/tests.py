import threading
import uuid
from datetime import date, datetime, timedelta
from datetime import timezone as dt_timezone
from decimal import Decimal
from unittest import mock

from django.db import IntegrityError, connection, transaction
from django.test import TestCase, TransactionTestCase
from django.urls import reverse
from rest_framework_simplejwt.tokens import RefreshToken

from apps.accounts.models import User
from apps.core.testing import HttpsAPIClient
from apps.inventory import stock
from apps.inventory.models import Ingredient, MovementKind, StockMovement, Unit
from apps.inventory.stock import stock_problems
from apps.inventory.tests import add_movement, count_data, receive_data
from apps.menu.models import MenuItem, RecipeLine
from apps.organizations.models import Membership, Organization, Role
from apps.organizations.testing import PASSWORD, TenantTestCase, make_user, org_url

from . import services
from .models import WasteDeduction, WasteEntry
from .services import VOID_NAMESPACE, WASTE_NAMESPACE, log_waste, void_waste

# 20:00 UTC on 10 November: still the 10th in UTC, already the 11th in India.
NOW = datetime(2026, 11, 10, 20, 0, tzinfo=dt_timezone.utc)
TODAY = date(2026, 11, 10)


def at(moment):
    return mock.patch("django.utils.timezone.now", return_value=moment)


class WasteTestCase(TenantTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.flour = Ingredient.objects.create(organization=cls.org_a, name="Flour", unit=Unit.KILOGRAM)
        cls.milk = Ingredient.objects.create(organization=cls.org_a, name="Milk", unit=Unit.LITRE)
        cls.lasagne = MenuItem.objects.create(organization=cls.org_a, name="Lasagne")
        cls.water = MenuItem.objects.create(organization=cls.org_a, name="Water")
        RecipeLine.objects.create(menu_item=cls.lasagne, ingredient=cls.flour, quantity=200, unit="g")
        RecipeLine.objects.create(menu_item=cls.lasagne, ingredient=cls.milk, quantity=Decimal("0.1"), unit="l")
        cls.oil_b = Ingredient.objects.create(organization=cls.org_b, name="Oil", unit=Unit.LITRE)
        cls.salad_b = MenuItem.objects.create(organization=cls.org_b, name="Salad")

    def setUp(self):
        super().setUp()
        clock = at(NOW)
        clock.start()
        self.addCleanup(clock.stop)

    def log(self, user=None, organization=None, **data):
        self.authenticate(user or self.staff_a)
        data.setdefault("request_id", str(uuid.uuid4()))
        data.setdefault("reason", "spoiled")
        for key in ("ingredient", "menu_item", "batch"):
            if hasattr(data.get(key), "pk"):
                data[key] = str(data[key].pk)
        return self.client.post(org_url("waste-list", organization or self.org_a), data, format="json")

    def void(self, entry_id, user=None, reason="Typed 50 instead of 5"):
        self.authenticate(user or self.manager_a)
        return self.client.post(org_url("waste-void", self.org_a, pk=entry_id), {"reason": reason}, format="json")

    def assertConsistent(self, *ingredients):
        for ingredient in ingredients:
            ingredient.refresh_from_db()
            self.assertEqual(stock_problems(ingredient), [])


class ModelTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Bakery")
        self.flour = Ingredient.objects.create(organization=self.organization, name="Flour", unit=Unit.KILOGRAM)
        self.item = MenuItem.objects.create(organization=self.organization, name="Bread")

    def entry(self, **fields):
        values = {
            "organization": self.organization, "quantity": 1, "unit": "kg", "reason": "spoiled",
            "wasted_on": TODAY, "request_id": uuid.uuid4(), **fields,
        }
        return WasteEntry.objects.create(**values)

    def test_entry_rules(self):
        cases = {
            "no target": {},
            "two targets": {"ingredient": self.flour, "menu_item": self.item, "unit": "each"},
            "menu item not in portions": {"menu_item": self.item, "unit": "kg"},
            "bad reason": {"ingredient": self.flour, "reason": "lost"},
            "void without reason": {"ingredient": self.flour, "voided_at": NOW},
        }
        for name, fields in cases.items():
            with self.subTest(name), self.assertRaises(IntegrityError), transaction.atomic():
                self.entry(**fields)
        self.entry(ingredient=self.flour)
        self.entry(menu_item=self.item, unit="each")

    def test_deductions_add_up(self):
        entry = self.entry(ingredient=self.flour)
        with self.assertRaises(IntegrityError), transaction.atomic():
            WasteDeduction.objects.create(entry=entry, ingredient=self.flour, unit="kg", required=2, taken=0, shortfall=1)
        WasteDeduction.objects.create(entry=entry, ingredient=self.flour, unit="kg", required=2, taken=0, shortfall=2)


class IngredientWasteTests(WasteTestCase):
    def test_staff_log_waste_taken_earliest_expiry_first(self):
        late = add_movement(self.flour, "2", expires_on=date(2026, 11, 30)).created_batches.get()
        soon = add_movement(self.flour, "1", expires_on=date(2026, 11, 12)).created_batches.get()

        response = self.log(ingredient=self.flour, quantity="1.5", reason="spoiled", note="Mouldy sack")

        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertEqual((body["kind"], body["quantity"], body["unit"], body["wasted_on"]), ("ingredient", "1.500", "kg", "2026-11-10"))
        self.assertEqual(body["deductions"], [
            {"ingredient": {"id": str(self.flour.pk), "name": "Flour"}, "unit": "kg", "required": "1.500",
             "taken": "1.500", "shortfall": "0.000", "returned": False},
        ])
        soon.refresh_from_db()
        late.refresh_from_db()
        self.assertEqual((soon.quantity_remaining, late.quantity_remaining), (Decimal("0"), Decimal("1.5")))
        movement = StockMovement.objects.get(kind=MovementKind.WASTE)
        self.assertEqual(movement.request_id, uuid.uuid5(WASTE_NAMESPACE, f"{body['id']}:{self.flour.pk}"))
        self.assertEqual(movement.created_by, self.staff_a)
        self.assertConsistent(self.flour)

    def test_compatible_units_are_converted(self):
        add_movement(self.flour, "1")

        body = self.log(ingredient=self.flour, quantity="300", unit="g").json()

        self.assertEqual((body["quantity"], body["unit"]), ("300.000", "g"))
        self.assertEqual(body["deductions"][0]["required"], "0.300")

    def test_rejects_incompatible_and_vanishing_quantities(self):
        add_movement(self.flour, "1")

        self.assertEqual(self.log(ingredient=self.flour, quantity="1", unit="l").status_code, 400)
        self.assertEqual(self.log(ingredient=self.flour, quantity="0.4", unit="g").status_code, 400)
        for quantity in ("0", "-1", "1.0001", "lots"):
            with self.subTest(quantity=quantity):
                self.assertEqual(self.log(ingredient=self.flour, quantity=quantity).status_code, 400)
        self.assertFalse(WasteEntry.objects.exists())

    def test_discarding_from_one_batch(self):
        soon = add_movement(self.flour, "1", expires_on=date(2026, 11, 12)).created_batches.get()
        later = add_movement(self.flour, "2", expires_on=date(2026, 11, 30)).created_batches.get()

        response = self.log(ingredient=self.flour, batch=later, quantity="2", reason="quality")

        self.assertEqual(response.status_code, 201)
        soon.refresh_from_db()
        later.refresh_from_db()
        self.assertEqual((soon.quantity_remaining, later.quantity_remaining), (Decimal("1"), Decimal("0")))
        self.assertEqual(response.json()["batch"]["id"], str(later.pk))
        self.assertConsistent(self.flour)

    def test_batch_must_belong_to_the_ingredient_and_hold_enough(self):
        flour_batch = add_movement(self.flour, "1").created_batches.get()
        milk_batch = add_movement(self.milk, "1").created_batches.get()
        oil_batch = add_movement(self.oil_b, "1").created_batches.get()

        too_much = self.log(ingredient=self.flour, batch=flour_batch, quantity="1.5")
        wrong_ingredient = self.log(ingredient=self.flour, batch=milk_batch, quantity="0.5")
        other_tenant = self.log(ingredient=self.flour, batch=oil_batch, quantity="0.5")

        self.assertEqual(too_much.status_code, 400)
        self.assertIn("holds only 1.000", too_much.json()["detail"])
        self.assertEqual(wrong_ingredient.status_code, 400)
        self.assertEqual(other_tenant.status_code, 400)
        self.assertFalse(WasteEntry.objects.exists())
        self.assertConsistent(self.flour)

    def test_archived_ingredients_cannot_change_stock(self):
        add_movement(self.flour, "1")
        self.flour.is_active = False
        self.flour.save()

        response = self.log(ingredient=self.flour, quantity="0.5")

        self.assertEqual(response.status_code, 400)
        self.assertFalse(WasteEntry.objects.exists())

    def test_shortfall_must_be_acknowledged(self):
        add_movement(self.flour, "0.5")

        refused = self.log(ingredient=self.flour, quantity="2")
        self.assertEqual(refused.status_code, 409)
        self.assertEqual(refused.json()["missing_acknowledgements"], ["shortfall"])
        self.assertEqual(refused.json()["warnings"][0]["details"][0]["shortfall"], "1.500")
        self.assertFalse(WasteEntry.objects.exists())

        accepted = self.log(ingredient=self.flour, quantity="2", acknowledged=["shortfall"])

        self.assertEqual(accepted.status_code, 201)
        deduction = WasteDeduction.objects.get()
        self.assertEqual((deduction.required, deduction.taken, deduction.shortfall), (Decimal("2"), Decimal("0.5"), Decimal("1.5")))
        self.assertConsistent(self.flour)

    def test_no_stock_at_all_records_only_a_shortfall(self):
        response = self.log(ingredient=self.flour, quantity="1", acknowledged=["shortfall"])

        self.assertEqual(response.status_code, 201)
        deduction = WasteDeduction.objects.get()
        self.assertEqual((deduction.taken, deduction.movement), (Decimal("0"), None))
        self.assertFalse(StockMovement.objects.filter(kind=MovementKind.WASTE).exists())

    def test_waste_date_range(self):
        add_movement(self.flour, "10")
        cases = [(TODAY + timedelta(days=1), 400), (TODAY - timedelta(days=8), 400), (TODAY - timedelta(days=7), 201), (TODAY, 201)]
        for wasted_on, expected in cases:
            with self.subTest(wasted_on=wasted_on):
                self.assertEqual(self.log(ingredient=self.flour, quantity="0.1", wasted_on=wasted_on.isoformat()).status_code, expected)

    def test_default_date_is_the_kitchens_today(self):
        add_movement(self.flour, "1")
        self.org_a.time_zone = "Asia/Kolkata"
        self.org_a.save()

        self.assertEqual(self.log(ingredient=self.flour, quantity="0.1").json()["wasted_on"], "2026-11-11")

    def test_retries_are_recognised(self):
        add_movement(self.flour, "5")
        data = {"ingredient": self.flour, "quantity": "1", "request_id": str(uuid.uuid4())}

        first = self.log(**data)
        retry = self.log(**data)
        different = self.log(**{**data, "quantity": "2"})

        self.assertEqual(first.status_code, 201)
        self.assertEqual(retry.status_code, 200)
        self.assertEqual(retry.json()["id"], first.json()["id"])
        self.assertEqual(different.status_code, 409)
        self.flour.refresh_from_db()
        self.assertEqual(self.flour.quantity_on_hand, Decimal("4"))

    def test_retry_after_midnight_without_a_date_is_still_recognised(self):
        add_movement(self.flour, "5")
        data = {"ingredient": self.flour, "quantity": "1", "request_id": str(uuid.uuid4())}
        self.log(**data)

        with at(NOW + timedelta(days=1)):
            self.assertEqual(self.log(**data).status_code, 200)

    def test_other_tenants(self):
        add_movement(self.oil_b, "5")

        self.assertEqual(self.log(ingredient=self.oil_b, quantity="1").status_code, 400)
        self.assertEqual(self.log(user=self.owner_b, ingredient=self.flour, quantity="1").status_code, 404)
        self.assertEqual(self.log(user=self.outsider, ingredient=self.flour, quantity="1").status_code, 404)
        self.oil_b.refresh_from_db()
        self.assertEqual(self.oil_b.quantity_on_hand, Decimal("5"))


class CountedAfterTests(WasteTestCase):
    """
    A count after the waste happened may already leave out what was thrown
    away. Waste for today happened when it was logged; waste for an earlier
    day may have happened any time that day, so from its start (kitchen-local).
    """

    def count_at(self, moment):
        with at(moment):
            add_movement(self.milk, "5")
            add_movement(self.milk, "4", kind=MovementKind.COUNT)

    def test_todays_waste_is_not_flagged_by_an_earlier_count_today(self):
        self.count_at(NOW - timedelta(hours=10))

        self.assertEqual(self.log(ingredient=self.milk, quantity="1").status_code, 201)

    def test_earlier_waste_is_flagged_by_any_count_since_the_start_of_that_day(self):
        self.count_at(datetime(2026, 11, 9, 8, 0, tzinfo=dt_timezone.utc))

        flagged = self.log(ingredient=self.milk, quantity="1", wasted_on="2026-11-09")
        not_flagged = self.log(ingredient=self.milk, quantity="1", wasted_on="2026-11-10")

        self.assertEqual(flagged.status_code, 409)
        self.assertEqual(flagged.json()["missing_acknowledgements"], ["counted_after"])
        self.assertIn("count it twice", flagged.json()["warnings"][0]["message"])
        self.assertEqual(not_flagged.status_code, 201)
        acknowledged = self.log(ingredient=self.milk, quantity="1", wasted_on="2026-11-09", acknowledged=["counted_after"])
        self.assertEqual(acknowledged.status_code, 201)

    def test_start_of_day_is_kitchen_local(self):
        self.org_a.time_zone = "Asia/Kolkata"
        self.org_a.save()
        # 18:00 UTC on the 9th is 23:30 that day in India: before 10 November began there.
        self.count_at(datetime(2026, 11, 9, 18, 0, tzinfo=dt_timezone.utc))

        self.assertEqual(self.log(ingredient=self.milk, quantity="1", wasted_on="2026-11-10").status_code, 201)
        self.assertEqual(self.log(ingredient=self.milk, quantity="1", wasted_on="2026-11-09").status_code, 409)


class MenuItemWasteTests(WasteTestCase):
    def test_portions_deduct_the_recipe(self):
        add_movement(self.flour, "5")
        add_movement(self.milk, "5")

        response = self.log(menu_item=self.lasagne, quantity="5", reason="overproduction")

        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertEqual((body["kind"], body["quantity"], body["unit"]), ("menu_item", "5.000", "each"))
        self.assertEqual(
            {(d["ingredient"]["name"], d["unit"], d["required"]) for d in body["deductions"]},
            {("Flour", "kg", "1.000"), ("Milk", "l", "0.500")},
        )
        self.assertConsistent(self.flour, self.milk)

    def test_menu_items_are_wasted_in_portions(self):
        self.assertEqual(self.log(menu_item=self.lasagne, quantity="1", unit="kg").status_code, 400)
        self.assertEqual(self.log(menu_item=self.lasagne, quantity="1", unit="each", acknowledged=["shortfall"]).status_code, 201)

    def test_batches_are_for_ingredients_only(self):
        batch = add_movement(self.flour, "5").created_batches.get()

        self.assertEqual(self.log(menu_item=self.lasagne, batch=batch, quantity="1").status_code, 400)

    def test_items_without_a_recipe_are_logged_without_deductions(self):
        response = self.log(menu_item=self.water, quantity="3")

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["deductions"], [])

    def test_exactly_one_target_and_tenant_checks(self):
        self.assertEqual(self.log(quantity="1").status_code, 400)
        self.assertEqual(self.log(ingredient=self.flour, menu_item=self.lasagne, quantity="1").status_code, 400)
        self.assertEqual(self.log(menu_item=self.salad_b, quantity="1").status_code, 400)


class VoidTests(WasteTestCase):
    def test_void_puts_back_exactly_what_was_taken_into_the_same_batches(self):
        soon = add_movement(self.flour, "1", expires_on=date(2026, 11, 12)).created_batches.get()
        later = add_movement(self.flour, "2", expires_on=date(2026, 11, 30)).created_batches.get()
        entry = self.log(ingredient=self.flour, quantity="1.5").json()["id"]
        # Stock moves on before the mistake is noticed.
        add_movement(self.flour, "-1", kind=MovementKind.COUNT)

        response = self.void(entry)

        self.assertEqual(response.status_code, 200)
        self.assertIs(response.json()["voided_now"], True)
        self.assertEqual(response.json()["void_reason"], "Typed 50 instead of 5")
        self.assertIs(response.json()["deductions"][0]["returned"], True)
        soon.refresh_from_db()
        later.refresh_from_db()
        # 1.5 was taken as 1 from `soon` and 0.5 from `later`; the count then took 1 more from `later`.
        self.assertEqual((soon.quantity_remaining, later.quantity_remaining), (Decimal("1"), Decimal("1")))
        reversal = StockMovement.objects.get(kind=MovementKind.WASTE_VOID)
        self.assertEqual(reversal.quantity_change, Decimal("1.5"))
        self.assertEqual(reversal.request_id, uuid.uuid5(VOID_NAMESPACE, f"{entry}:{self.flour.pk}"))
        self.assertEqual(
            sorted(reversal.lines.values_list("batch_id", "quantity_change")),
            sorted([(soon.pk, Decimal("1")), (later.pk, Decimal("0.5"))]),
        )
        self.assertConsistent(self.flour)

    def test_voiding_twice_changes_nothing(self):
        add_movement(self.flour, "2")
        entry = self.log(ingredient=self.flour, quantity="1").json()["id"]

        self.void(entry)
        again = self.void(entry, reason="Again")

        self.assertEqual(again.status_code, 200)
        self.assertIs(again.json()["voided_now"], False)
        self.assertEqual(again.json()["void_reason"], "Typed 50 instead of 5")
        self.assertEqual(StockMovement.objects.filter(kind=MovementKind.WASTE_VOID).count(), 1)
        self.flour.refresh_from_db()
        self.assertEqual(self.flour.quantity_on_hand, Decimal("2"))

    def test_void_of_a_shortfall_returns_only_what_was_taken(self):
        add_movement(self.flour, "0.5")
        entry = self.log(ingredient=self.flour, quantity="2", acknowledged=["shortfall"]).json()["id"]

        self.void(entry)

        self.flour.refresh_from_db()
        self.assertEqual(self.flour.quantity_on_hand, Decimal("0.5"))
        self.assertConsistent(self.flour)

    def test_menu_item_void_returns_every_ingredient(self):
        add_movement(self.flour, "5")
        add_movement(self.milk, "5")
        entry = self.log(menu_item=self.lasagne, quantity="5").json()["id"]

        self.void(entry)

        self.flour.refresh_from_db()
        self.milk.refresh_from_db()
        self.assertEqual((self.flour.quantity_on_hand, self.milk.quantity_on_hand), (Decimal("5"), Decimal("5")))
        self.assertConsistent(self.flour, self.milk)

    def test_permissions_tenants_and_reason(self):
        add_movement(self.flour, "2")
        entry = self.log(ingredient=self.flour, quantity="1").json()["id"]

        self.assertEqual(self.void(entry, user=self.staff_a).status_code, 403)
        self.assertEqual(self.void(entry, user=self.owner_b).status_code, 404)
        self.assertEqual(self.void(entry, reason="").status_code, 400)
        self.assertFalse(WasteEntry.objects.get().voided_at)
        self.assertEqual(self.void(entry, user=self.owner_a).status_code, 200)

    def test_archived_ingredients_block_voids(self):
        add_movement(self.flour, "2")
        entry = self.log(ingredient=self.flour, quantity="1").json()["id"]
        Ingredient.objects.filter(pk=self.flour.pk).update(is_active=False)

        self.assertEqual(self.void(entry).status_code, 400)
        self.assertIsNone(WasteEntry.objects.get().voided_at)

    def test_voided_entries_leave_lists_unless_asked_for(self):
        add_movement(self.flour, "2")
        entry = self.log(ingredient=self.flour, quantity="1").json()["id"]
        self.void(entry)
        self.authenticate(self.staff_a)

        default = self.client.get(org_url("waste-list", self.org_a)).json()
        everything = self.client.get(org_url("waste-list", self.org_a), {"include_voided": "true"}).json()

        self.assertEqual(default["count"], 0)
        self.assertEqual(everything["results"][0]["voided_by"], "manager@a.example")


class AtomicityTests(WasteTestCase):
    def test_failure_midway_through_logging_leaves_nothing(self):
        flour_batch = add_movement(self.flour, "5").created_batches.get()
        add_movement(self.milk, "5")
        real = stock.record_stock_change
        calls = []

        def fail_on_second(*args, **kwargs):
            calls.append(kwargs["kind"])
            if len(calls) == 2:
                raise RuntimeError("database went away")
            return real(*args, **kwargs)

        data = {
            "request_id": uuid.uuid4(), "menu_item": self.lasagne, "quantity": Decimal("5"), "unit": "each",
            "reason": "overproduction", "note": "", "acknowledged": [],
        }
        with mock.patch("apps.waste.services.record_stock_change", side_effect=fail_on_second):
            with self.assertRaises(RuntimeError):
                log_waste(self.org_a, self.staff_a, data)

        self.assertEqual(len(calls), 2)  # the first ingredient was deducted before the failure
        self.assertFalse(WasteEntry.objects.exists())
        self.assertFalse(WasteDeduction.objects.exists())
        self.assertFalse(StockMovement.objects.filter(kind=MovementKind.WASTE).exists())
        flour_batch.refresh_from_db()
        self.assertEqual(flour_batch.quantity_remaining, Decimal("5"))
        self.assertConsistent(self.flour, self.milk)

    def test_failure_midway_through_voiding_leaves_it_unvoided(self):
        add_movement(self.flour, "5")
        add_movement(self.milk, "5")
        entry = self.log(menu_item=self.lasagne, quantity="5").json()["id"]
        real = stock.return_taken_stock
        calls = []

        def fail_on_second(*args, **kwargs):
            calls.append(1)
            if len(calls) == 2:
                raise RuntimeError("database went away")
            return real(*args, **kwargs)

        with mock.patch("apps.waste.services.return_taken_stock", side_effect=fail_on_second):
            with self.assertRaises(RuntimeError):
                void_waste(self.org_a, entry, self.manager_a, "mistake")

        self.assertEqual(len(calls), 2)
        self.assertIsNone(WasteEntry.objects.get().voided_at)
        self.assertFalse(StockMovement.objects.filter(kind=MovementKind.WASTE_VOID).exists())
        self.assertFalse(WasteDeduction.objects.filter(reversal__isnull=False).exists())
        self.flour.refresh_from_db()
        self.milk.refresh_from_db()
        self.assertEqual((self.flour.quantity_on_hand, self.milk.quantity_on_hand), (Decimal("4"), Decimal("4.5")))
        self.assertConsistent(self.flour, self.milk)
        self.assertEqual(self.void(entry).status_code, 200)


class SummaryTests(WasteTestCase):
    def summary(self, **params):
        self.authenticate(self.staff_a)
        return self.client.get(org_url("waste-summary", self.org_a), params)

    def test_groups_quantities_by_unit_and_never_across_units(self):
        add_movement(self.flour, "10")
        add_movement(self.milk, "10")
        self.log(ingredient=self.flour, quantity="500", unit="g", reason="spoiled")
        self.log(ingredient=self.milk, quantity="1", reason="spoiled")
        self.log(menu_item=self.lasagne, quantity="5", reason="overproduction")
        self.log(menu_item=self.water, quantity="2", reason="overproduction")
        voided = self.log(ingredient=self.flour, quantity="3", reason="spoiled").json()["id"]
        self.void(voided)

        body = self.summary().json()

        self.assertEqual((body["from"], body["to"], body["entries"]), ("2026-11-04", "2026-11-10", 4))
        self.assertEqual(
            [(row["ingredient"]["name"], row["unit"], row["quantity"], row["entries"]) for row in body["by_ingredient"]],
            [("Flour", "kg", "1.500", 2), ("Milk", "l", "1.500", 2)],
        )
        self.assertEqual(
            body["by_reason"],
            [
                {"reason": "overproduction", "entries": 2,
                 "quantities": [{"unit": "kg", "quantity": "1.000"}, {"unit": "l", "quantity": "0.500"}],
                 "portions": "7.000"},
                {"reason": "spoiled", "entries": 2,
                 "quantities": [{"unit": "kg", "quantity": "0.500"}, {"unit": "l", "quantity": "1.000"}],
                 "portions": "0.000"},
            ],
        )
        self.assertEqual(
            [(row["menu_item"]["name"], row["portions"]) for row in body["by_menu_item"]],
            [("Lasagne", "5.000"), ("Water", "2.000")],
        )

    def test_unit_changes_never_mix_in_totals(self):
        sugar = Ingredient.objects.create(organization=self.org_a, name="Sugar", unit=Unit.GRAM)
        self.log(ingredient=sugar, quantity="100", acknowledged=["shortfall"])
        # No stock history yet, so the unit may still change.
        Ingredient.objects.filter(pk=sugar.pk).update(unit=Unit.KILOGRAM)
        self.log(ingredient=sugar, quantity="0.5", acknowledged=["shortfall"])

        rows = [(r["unit"], r["quantity"]) for r in self.summary().json()["by_ingredient"] if r["ingredient"]["name"] == "Sugar"]

        self.assertEqual(rows, [("g", "100.000"), ("kg", "0.500")])

    def test_dates_are_kitchen_local_and_validated(self):
        self.org_a.time_zone = "Asia/Kolkata"
        self.org_a.save()

        self.assertEqual(self.summary().json()["to"], "2026-11-11")
        for params in ({"from": "nope"}, {"from": "2026-11-10", "to": "2026-11-01"}, {"from": "2026-01-01", "to": "2026-11-01"}):
            with self.subTest(params=params):
                self.assertEqual(self.summary(**params).status_code, 400)

    def test_other_tenants(self):
        self.authenticate(self.owner_b)

        self.assertEqual(self.client.get(org_url("waste-summary", self.org_a)).status_code, 404)


class ListTests(WasteTestCase):
    def test_filters(self):
        add_movement(self.flour, "10")
        add_movement(self.milk, "10")
        self.log(ingredient=self.flour, quantity="1", reason="spoiled")
        self.log(ingredient=self.milk, quantity="1", reason="damaged", wasted_on="2026-11-08")
        self.log(menu_item=self.lasagne, quantity="1", reason="overproduction")
        url = org_url("waste-list", self.org_a)

        def names(**params):
            return sorted((e["ingredient"] or e["menu_item"])["name"] for e in self.client.get(url, params).json()["results"])

        self.assertEqual(names(), ["Flour", "Lasagne", "Milk"])
        self.assertEqual(names(reason="damaged"), ["Milk"])
        self.assertEqual(names(ingredient=str(self.flour.pk)), ["Flour", "Lasagne"])
        self.assertEqual(names(**{"from": "2026-11-09"}), ["Flour", "Lasagne"])
        self.assertEqual(self.client.get(url, {"reason": "lost"}).status_code, 400)
        self.assertEqual(self.client.get(url, {"ingredient": "nope"}).status_code, 400)

    def test_detail_and_tenants(self):
        add_movement(self.flour, "1")
        entry = self.log(ingredient=self.flour, quantity="1").json()["id"]

        self.assertEqual(self.client.get(org_url("waste-detail", self.org_a, pk=entry)).status_code, 200)
        self.authenticate(self.owner_b)
        self.assertEqual(self.client.get(org_url("waste-detail", self.org_a, pk=entry)).status_code, 404)
        self.assertEqual(self.client.get(org_url("waste-detail", self.org_b, pk=entry)).status_code, 404)

    def test_wasted_ingredients_and_menu_items_cannot_be_deleted(self):
        sugar = Ingredient.objects.create(organization=self.org_a, name="Sugar", unit=Unit.GRAM)
        self.log(ingredient=sugar, quantity="1", acknowledged=["shortfall"])
        self.log(menu_item=self.water, quantity="1")
        self.authenticate(self.owner_a)

        self.assertEqual(self.client.delete(org_url("ingredient-detail", self.org_a, pk=sugar.pk)).status_code, 400)
        self.assertEqual(self.client.delete(org_url("menu-item-detail", self.org_a, pk=self.water.pk)).status_code, 400)


class AdminTests(TestCase):
    def test_entries_are_view_only(self):
        admin_user = User.objects.create_superuser(email="admin@example.com", password=PASSWORD)
        organization = Organization.objects.create(name="Bakery")
        flour = Ingredient.objects.create(organization=organization, name="Flour", unit=Unit.KILOGRAM)
        entry = WasteEntry.objects.create(
            organization=organization, ingredient=flour, quantity=1, unit="kg", reason="spoiled",
            wasted_on=TODAY, request_id=uuid.uuid4(),
        )
        self.client.force_login(admin_user)
        change = reverse("admin:waste_wasteentry_change", args=[entry.pk])

        self.assertEqual(self.client.get(change, secure=True).status_code, 200)
        self.assertEqual(self.client.post(change, {"reason": "other"}, secure=True).status_code, 403)
        self.assertEqual(
            self.client.post(reverse("admin:waste_wasteentry_delete", args=[entry.pk]), {"post": "yes"}, secure=True).status_code,
            403,
        )
        entry.refresh_from_db()
        self.assertEqual(entry.reason, "spoiled")


class ConcurrentWasteTests(TransactionTestCase):
    """Parallel requests, each thread with its own PostgreSQL connection."""

    def setUp(self):
        self.assertEqual(connection.vendor, "postgresql")
        self.organization = Organization.objects.create(name="Bakery")
        self.cook = make_user("cook@example.com")
        self.manager = make_user("manager@example.com")
        Membership.objects.create(user=self.cook, organization=self.organization, role=Role.STAFF)
        Membership.objects.create(user=self.manager, organization=self.organization, role=Role.MANAGER)
        self.flour = Ingredient.objects.create(organization=self.organization, name="Flour", unit=Unit.KILOGRAM)
        self.milk = Ingredient.objects.create(organization=self.organization, name="Milk", unit=Unit.LITRE)
        self.lasagne = MenuItem.objects.create(organization=self.organization, name="Lasagne")
        RecipeLine.objects.create(menu_item=self.lasagne, ingredient=self.flour, quantity=200, unit="g")
        RecipeLine.objects.create(menu_item=self.lasagne, ingredient=self.milk, quantity=Decimal("0.1"), unit="l")
        add_movement(self.flour, "20")
        add_movement(self.milk, "20")

    def run_together(self, *requests):
        barrier = threading.Barrier(len(requests))
        results = [None] * len(requests)

        def send(index, user, url, data):
            client = HttpsAPIClient()
            client.credentials(HTTP_AUTHORIZATION=f"Bearer {RefreshToken.for_user(user).access_token}")
            try:
                barrier.wait(timeout=10)
                response = client.post(url, data, format="json")
                results[index] = (response.status_code, response.json())
            finally:
                connection.close()

        threads = [threading.Thread(target=send, args=(i, *request)) for i, request in enumerate(requests)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        return results

    def waste_url(self):
        return org_url("waste-list", self.organization)

    def test_simultaneous_duplicate_submissions_log_once(self):
        data = {"request_id": str(uuid.uuid4()), "menu_item": str(self.lasagne.pk), "quantity": "10", "reason": "overproduction"}

        results = self.run_together(*[(self.cook, self.waste_url(), data)] * 4)

        self.assertEqual(sorted(status for status, _ in results), [200, 200, 200, 201])
        self.assertEqual(len({body["id"] for _, body in results}), 1)
        self.assertEqual(WasteEntry.objects.count(), 1)
        for ingredient, expected in ((self.flour, "18"), (self.milk, "19")):
            ingredient.refresh_from_db()
            self.assertEqual(ingredient.quantity_on_hand, Decimal(expected))
            self.assertEqual(stock_problems(ingredient), [])

    def test_simultaneous_voids_return_stock_once(self):
        client = HttpsAPIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {RefreshToken.for_user(self.cook).access_token}")
        entry = client.post(
            self.waste_url(),
            {"request_id": str(uuid.uuid4()), "menu_item": str(self.lasagne.pk), "quantity": "10", "reason": "overproduction"},
            format="json",
        ).json()["id"]
        url = org_url("waste-void", self.organization, pk=entry)

        results = self.run_together(*[(self.manager, url, {"reason": "mistake"})] * 3)

        self.assertEqual([status for status, _ in results], [200] * 3)
        self.assertEqual(sorted(body["voided_now"] for _, body in results), [False, False, True])
        for ingredient in (self.flour, self.milk):
            ingredient.refresh_from_db()
            self.assertEqual(ingredient.quantity_on_hand, Decimal("20"))
            self.assertEqual(stock_problems(ingredient), [])

    def test_waste_during_deliveries_and_counts_keeps_stock_consistent(self):
        receive = org_url("ingredient-receive", self.organization, pk=self.flour.pk)
        count = org_url("ingredient-count", self.organization, pk=self.milk.pk)
        # If the count commits while the waste waits for its lock, the count is
        # genuinely later than the waste and the warning applies; acknowledge it.
        waste = {
            "request_id": str(uuid.uuid4()), "menu_item": str(self.lasagne.pk), "quantity": "10",
            "reason": "overproduction", "acknowledged": ["counted_after"],
        }

        results = self.run_together(
            (self.cook, self.waste_url(), waste),
            (self.cook, receive, receive_data("3")),
            (self.cook, count, count_data("19.5")),
            (self.cook, receive, receive_data("1")),
        )

        # The count may land before or after the waste; either way nothing is lost or duplicated.
        self.assertEqual(sorted(status for status, _ in results), [201, 201, 201, 201])
        self.flour.refresh_from_db()
        self.assertEqual(self.flour.quantity_on_hand, Decimal("22"))
        for ingredient in (self.flour, self.milk):
            ingredient.refresh_from_db()
            self.assertEqual(stock_problems(ingredient), [])
