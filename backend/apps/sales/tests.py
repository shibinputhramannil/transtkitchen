import threading
import uuid
from datetime import date, datetime, timedelta
from datetime import timezone as dt_timezone
from decimal import Decimal
from unittest import mock
from zoneinfo import ZoneInfo

from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test import SimpleTestCase, TestCase, TransactionTestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from rest_framework_simplejwt.tokens import RefreshToken

from apps.accounts.models import User
from apps.core.testing import HttpsAPIClient
from apps.inventory.models import Ingredient, MovementKind, StockMovement, Unit
from apps.inventory.stock import record_stock_change, stock_problems
from apps.inventory.tests import add_movement, receive_data
from apps.menu.models import MenuItem, MenuItemAlias, RecipeLine
from apps.organizations.models import Membership, Organization, Role
from apps.organizations.testing import PASSWORD, TenantTestCase, make_user, org_url

from . import services
from .csv_import import MAX_FILE_BYTES, MAX_ROWS, ImportFileError, parse_sales_csv
from .models import ImportDeduction, ImportStatus, SaleLine, SalesImport
from .services import DEDUCTION_NAMESPACE, apply_import

# 20:00 UTC on 10 November: still the 10th in UTC, already the 11th in India.
NOW = datetime(2026, 11, 10, 20, 0, tzinfo=dt_timezone.utc)
UTC = ZoneInfo("UTC")
KOLKATA = ZoneInfo("Asia/Kolkata")


def at(moment):
    return mock.patch("django.utils.timezone.now", return_value=moment)


def csv_upload(text, name="sales.csv"):
    return SimpleUploadedFile(name, text.encode("utf-8"), content_type="text/csv")


def parse(text, zone=UTC, today=date(2026, 11, 10)):
    return parse_sales_csv(text.encode("utf-8"), zone, today)


class ParsingTests(SimpleTestCase):
    def test_reads_headers_in_any_case_with_a_byte_order_mark(self):
        data = "﻿ Date ,ITEM,Quantity,Item_ID,notes\n2026-11-09,Pizza,3,P1,ignored\n".encode("utf-8")

        rows = parse_sales_csv(data, UTC, date(2026, 11, 10)).rows

        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual((row.business_date, row.item_name, row.quantity, row.external_id),
                         (date(2026, 11, 9), "Pizza", Decimal("3"), "P1"))
        self.assertEqual(row.errors, [])

    def test_item_id_is_optional_and_blank_lines_are_skipped(self):
        rows = parse("date,item,quantity\n2026-11-09,Pizza,1\n\n2026-11-09,Tea,2\n").rows

        self.assertEqual([(row.row_number, row.item_name) for row in rows], [(2, "Pizza"), (4, "Tea")])

    def test_rejects_unusable_files(self):
        cases = {
            "": "empty",
            "date,item,quantity\n": "no sales rows",
            "date,item\n2026-11-09,Pizza\n": "Missing required columns: quantity",
            "date,item,quantity,Item\n2026-11-09,Pizza,1,x\n": "Duplicate column headers: item",
            "date,,item,quantity\n2026-11-09,,Pizza,1\n": "empty one",
            'date,item,quantity\n2026-11-09,"Pizza,1\n': "not valid CSV",
        }
        for text, message in cases.items():
            with self.subTest(text=text):
                with self.assertRaisesMessage(ImportFileError, message):
                    parse(text)

    def test_rejects_non_utf8_and_binary_files(self):
        with self.assertRaisesMessage(ImportFileError, "not UTF-8"):
            parse_sales_csv("date,item,quantity\n2026-11-09,Crème,1\n".encode("latin-1"), UTC, date(2026, 11, 10))
        with self.assertRaisesMessage(ImportFileError, "not a text CSV"):
            parse_sales_csv(b"date,item,quantity\n\x00\x01", UTC, date(2026, 11, 10))

    def test_row_limit(self):
        row = "2026-11-09,Pizza,1\n"
        self.assertEqual(len(parse("date,item,quantity\n" + row * MAX_ROWS).rows), MAX_ROWS)
        with self.assertRaisesMessage(ImportFileError, f"more than {MAX_ROWS} rows"):
            parse("date,item,quantity\n" + row * (MAX_ROWS + 1))

    def test_row_errors(self):
        text = (
            "date,item,quantity\n"
            "2026-11-09,Pizza\n"
            "2026-11-09,,1\n"
            "09/11/2026,Pizza,1\n"
            "2026-11-13,Pizza,1\n"
            "2026-11-09,Pizza,0\n"
            "2026-11-09,Pizza,-2\n"
            "2026-11-09,Pizza,1.0001\n"
            "2026-11-09,Pizza,lots\n"
            "2026-11-09,Pizza,NaN\n"
            f"2026-11-09,{'x' * 201},1\n"
        )

        errors = [row.error for row in parse(text).rows]

        expected = [
            "Expected 3 columns",
            "item is empty",
            "is not a date",
            "in the future",
            "greater than zero",
            "greater than zero",
            "decimal places",
            "is not a number",
            "greater than zero",
            "longer than 200",
        ]
        for error, fragment in zip(errors, expected, strict=True):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, error)

    def test_dates_use_the_kitchens_time_zone(self):
        text = (
            "date,item,quantity\n"
            "2026-11-10,A,1\n"
            "2026-11-10T20:30:00Z,B,1\n"
            "2026-11-10T20:30:00+00:00,C,1\n"
            "2026-11-10T09:15,D,1\n"
        )

        rows = parse(text, zone=KOLKATA, today=date(2026, 11, 11)).rows

        self.assertEqual(rows[0].business_date, date(2026, 11, 10))
        self.assertIsNone(rows[0].sold_at)
        # 20:30 UTC is 02:00 the next morning in India.
        self.assertEqual(rows[1].business_date, date(2026, 11, 11))
        self.assertEqual(rows[2].business_date, date(2026, 11, 11))
        # Without an offset, the time is kitchen-local.
        self.assertEqual(rows[3].business_date, date(2026, 11, 10))
        self.assertEqual(rows[3].sold_at, datetime(2026, 11, 10, 9, 15, tzinfo=KOLKATA))


class SalesTestCase(TenantTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.flour = Ingredient.objects.create(organization=cls.org_a, name="Flour", unit=Unit.KILOGRAM)
        cls.milk = Ingredient.objects.create(organization=cls.org_a, name="Milk", unit=Unit.LITRE)
        cls.pizza = MenuItem.objects.create(organization=cls.org_a, name="Margherita Pizza")
        cls.latte = MenuItem.objects.create(organization=cls.org_a, name="Latte")
        cls.water = MenuItem.objects.create(organization=cls.org_a, name="Water")
        RecipeLine.objects.create(menu_item=cls.pizza, ingredient=cls.flour, quantity=250, unit="g")
        RecipeLine.objects.create(menu_item=cls.latte, ingredient=cls.milk, quantity=Decimal("0.2"), unit="l")
        cls.salad_b = MenuItem.objects.create(organization=cls.org_b, name="Salad")

    def setUp(self):
        super().setUp()
        clock = at(NOW)
        clock.start()
        self.addCleanup(clock.stop)

    def upload(self, text, user=None, name="sales.csv", organization=None):
        self.authenticate(user or self.manager_a)
        return self.client.post(
            org_url("sales-import-list", organization or self.org_a), {"file": csv_upload(text, name)}, format="multipart"
        )

    def apply(self, import_id, acknowledged=(), user=None):
        self.authenticate(user or self.manager_a)
        return self.client.post(
            org_url("sales-import-apply", self.org_a, pk=import_id),
            {"acknowledged": list(acknowledged)},
            format="json",
        )

    def warning_codes(self, preview):
        return [warning["code"] for warning in preview["warnings"]]


GOOD_CSV = "date,item,quantity\n2026-11-09,Margherita Pizza,4\n2026-11-09,Latte,10\n2026-11-10,margherita pizza,2\n"


class UploadTests(SalesTestCase):
    def test_manager_uploads_a_draft_with_a_preview(self):
        add_movement(self.flour, "10")
        add_movement(self.milk, "10")

        response = self.upload(GOOD_CSV, name="nov.csv")

        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertEqual(body["status"], "draft")
        self.assertEqual(body["file_name"], "nov.csv")
        self.assertEqual((body["first_date"], body["last_date"], body["row_count"]), ("2026-11-09", "2026-11-10", 3))
        self.assertEqual(body["summary"]["matched_rows"], 3)
        self.assertEqual(
            {(d["ingredient"]["name"], d["required"]) for d in body["deductions"]},
            {("Flour", "1.500"), ("Milk", "2.000")},
        )
        self.assertEqual(body["warnings"], [])
        self.assertIs(body["can_apply"], True)
        self.assertEqual(SaleLine.objects.count(), 3)

    def test_only_managers_and_owners_upload(self):
        self.assertEqual(self.upload(GOOD_CSV, user=self.staff_a).status_code, 403)
        self.assertEqual(self.upload(GOOD_CSV, user=self.owner_b).status_code, 404)
        self.assertFalse(SalesImport.objects.exists())

    def test_unusable_files_store_nothing(self):
        for text in ("date,item,item,quantity\n2026-11-09,a,b,1\n", "date,item,quantity\n" + "2026-11-09,P,1\n" * (MAX_ROWS + 1)):
            with self.subTest(text=text[:30]):
                response = self.upload(text)
                self.assertEqual(response.status_code, 400)
                self.assertIn("file", response.json())
        self.assertFalse(SalesImport.objects.exists())
        self.assertFalse(SaleLine.objects.exists())

    def test_oversized_files_are_refused_before_reading(self):
        big = "date,item,quantity\n" + "2026-11-09,Pizza,1\n" * (MAX_FILE_BYTES // 18 + 10)
        self.assertGreater(len(big), MAX_FILE_BYTES)

        response = self.upload(big)

        self.assertEqual(response.status_code, 400)
        self.assertIn("larger than 2 MB", response.json()["file"][0])
        self.assertFalse(SalesImport.objects.exists())

    def test_requires_a_file(self):
        self.authenticate(self.manager_a)

        self.assertEqual(self.client.post(org_url("sales-import-list", self.org_a), {}, format="multipart").status_code, 400)

    def test_rows_with_errors_are_kept_for_the_preview(self):
        response = self.upload("date,item,quantity\n2026-11-09,Latte,1\nyesterday,Latte,1\n")

        self.assertEqual(response.json()["summary"]["error_rows"], 1)
        self.assertIn("row_errors", self.warning_codes(response.json()))
        rows = self.client.get(org_url("sales-import-rows", self.org_a, pk=response.json()["id"]), {"status": "error"}).json()
        self.assertEqual([(row["row_number"], row["item_name"]) for row in rows["results"]], [(3, "Latte")])

    def test_names_are_returned_exactly_as_text(self):
        name = '<img src=x onerror="alert(1)">'

        response = self.upload(f'date,item,quantity\n2026-11-09,"{name.replace(chr(34), chr(34) * 2)}",1\n')

        self.assertEqual(response.json()["unmatched"][0]["item_name"], name)

    def test_same_file_twice_returns_the_same_draft(self):
        first = self.upload(GOOD_CSV)
        second = self.upload(GOOD_CSV, name="renamed.csv")

        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.json()["id"], first.json()["id"])
        self.assertIs(second.json()["duplicate"], True)
        self.assertEqual(SalesImport.objects.count(), 1)

    def test_same_file_after_apply_is_refused_and_after_discard_is_allowed(self):
        add_movement(self.flour, "10")
        add_movement(self.milk, "10")
        applied = self.upload(GOOD_CSV).json()["id"]
        self.apply(applied)

        self.assertEqual(self.upload(GOOD_CSV).status_code, 409)

        other = "date,item,quantity\n2026-11-08,Latte,1\n"
        draft = self.upload(other).json()["id"]
        self.client.post(org_url("sales-import-discard", self.org_a, pk=draft))
        self.assertEqual(self.upload(other).status_code, 201)

    def test_template(self):
        self.authenticate(self.staff_a)

        response = self.client.get(org_url("sales-import-template", self.org_a))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/csv; charset=utf-8")
        self.assertTrue(response.content.decode().startswith("date,item,quantity,item_id\n"))


class MatchingTests(SalesTestCase):
    def test_remembered_names_and_item_ids_match(self):
        MenuItemAlias.objects.create(organization=self.org_a, menu_item=self.pizza, source="csv", external_name="Marg 12in")
        MenuItemAlias.objects.create(
            organization=self.org_a, menu_item=self.latte, source="csv", external_name="Old latte name", external_id="SKU-9"
        )

        body = self.upload("date,item,quantity,item_id\n2026-11-09,MARG 12IN,1,\n2026-11-09,Café latte,1,SKU-9\n").json()

        self.assertEqual(body["summary"]["matched_rows"], 2)
        self.assertEqual({item["menu_item"]["name"] for item in body["items"]}, {"Margherita Pizza", "Latte"})

    def test_mapping_is_remembered_for_the_next_import(self):
        body = self.upload("date,item,quantity\n2026-11-09,Pizza Marg,2\n2026-11-09,pizza marg,1\n").json()
        self.assertEqual(body["unmatched"][0]["item_name"], "Pizza Marg")
        self.assertIn("unmatched_items", [b["code"] for b in body["blocking"]])

        mapped = self.client.post(
            org_url("sales-import-mappings", self.org_a, pk=body["id"]),
            {"item_name": "PIZZA MARG", "menu_item": str(self.pizza.pk)},
            format="json",
        ).json()

        self.assertEqual(mapped["summary"]["matched_rows"], 2)
        self.assertEqual(mapped["blocking"], [])
        later = self.upload("date,item,quantity\n2026-11-10,Pizza marg,1\n").json()
        self.assertEqual(later["summary"]["matched_rows"], 1)

    def test_ignoring_a_name_excludes_its_rows(self):
        body = self.upload("date,item,quantity\n2026-11-09,Gift card,1\n2026-11-09,Latte,1\n").json()

        ignored = self.client.post(
            org_url("sales-import-mappings", self.org_a, pk=body["id"]),
            {"item_name": "gift card", "ignore": True},
            format="json",
        ).json()

        self.assertEqual(ignored["summary"]["ignored_rows"], 1)
        self.assertEqual(ignored["blocking"], [])
        self.assertFalse(MenuItemAlias.objects.exists())

    def test_cannot_map_to_another_tenants_menu_item(self):
        body = self.upload("date,item,quantity\n2026-11-09,Salad,1\n").json()

        response = self.client.post(
            org_url("sales-import-mappings", self.org_a, pk=body["id"]),
            {"item_name": "Salad", "menu_item": str(self.salad_b.pk)},
            format="json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertFalse(MenuItemAlias.objects.exists())

    def test_mapping_validation(self):
        body = self.upload("date,item,quantity\n2026-11-09,Soup,1\n").json()
        url = org_url("sales-import-mappings", self.org_a, pk=body["id"])

        self.assertEqual(self.client.post(url, {"item_name": "Soup"}, format="json").status_code, 400)
        self.assertEqual(self.client.post(url, {"item_name": "Nope", "ignore": True}, format="json").status_code, 400)
        self.authenticate(self.staff_a)
        self.assertEqual(self.client.post(url, {"item_name": "Soup", "ignore": True}, format="json").status_code, 403)


class RematchTests(SalesTestCase):
    """Viewing a preview never changes the draft; re-matching is an explicit action."""

    def line_matches(self, import_id):
        return list(
            SaleLine.objects.filter(sales_import_id=import_id).order_by("row_number").values_list(
                "item_name", "menu_item_id", "ignored"
            )
        )

    def test_viewing_the_preview_writes_nothing(self):
        draft = self.upload("date,item,quantity\n2026-11-09,Soup,1\n2026-11-09,Latte,1\n").json()
        before = self.line_matches(draft["id"])
        updated_at = SalesImport.objects.get().updated_at
        # The menu changes after the upload: "Soup" could now match.
        MenuItem.objects.create(organization=self.org_a, name="Soup")
        self.authenticate(self.staff_a)

        with CaptureQueriesContext(connection) as queries:
            preview = self.client.get(org_url("sales-import-detail", self.org_a, pk=draft["id"])).json()

        writes = [
            q["sql"] for q in queries if q["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))
        ]
        self.assertEqual(writes, [])
        self.assertEqual(self.line_matches(draft["id"]), before)
        self.assertEqual(SalesImport.objects.get().updated_at, updated_at)
        # The preview shows the stored matches and says re-matching would change one row.
        self.assertEqual([entry["item_name"] for entry in preview["unmatched"]], ["Soup"])
        self.assertEqual(preview["rematch_available"], 1)

    def test_rematch_applies_current_matches_and_is_safe_to_repeat(self):
        draft = self.upload("date,item,quantity\n2026-11-09,Soup,2\n2026-11-09,Gift card,1\n").json()
        url = org_url("sales-import-mappings", self.org_a, pk=draft["id"])
        self.client.post(url, {"item_name": "Gift card", "ignore": True}, format="json")
        soup = MenuItem.objects.create(organization=self.org_a, name="Soup")
        rematch = org_url("sales-import-rematch", self.org_a, pk=draft["id"])

        first = self.client.post(rematch)
        second = self.client.post(rematch)

        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json()["rematched"], 1)
        self.assertEqual(first.json()["rematch_available"], 0)
        self.assertEqual(first.json()["blocking"], [])
        self.assertEqual(second.json()["rematched"], 0)
        # Ignored rows stay ignored.
        self.assertEqual(
            self.line_matches(draft["id"]), [("Soup", soup.pk, False), ("Gift card", None, True)]
        )

    def test_apply_uses_the_stored_matches_not_new_ones(self):
        draft = self.upload("date,item,quantity\n2026-11-09,Soup,1\n").json()
        MenuItem.objects.create(organization=self.org_a, name="Soup")

        refused = self.apply(draft["id"])

        # Applying does not quietly re-match: the stored row is still unmatched.
        self.assertEqual(refused.status_code, 400)
        self.assertEqual(self.line_matches(draft["id"]), [("Soup", None, False)])
        self.client.post(org_url("sales-import-rematch", self.org_a, pk=draft["id"]))
        self.assertEqual(self.apply(draft["id"]).status_code, 200)

    def test_mapping_re_matches_only_its_own_rows(self):
        draft = self.upload("date,item,quantity\n2026-11-09,Pizza Marg,1\n2026-11-09,Soup,1\n").json()
        MenuItem.objects.create(organization=self.org_a, name="Soup")

        mapped = self.client.post(
            org_url("sales-import-mappings", self.org_a, pk=draft["id"]),
            {"item_name": "Pizza Marg", "menu_item": str(self.pizza.pk)},
            format="json",
        ).json()

        self.assertEqual(self.line_matches(draft["id"]), [("Pizza Marg", self.pizza.pk, False), ("Soup", None, False)])
        self.assertEqual(mapped["rematch_available"], 1)

    def test_rematch_roles_states_and_tenants(self):
        add_movement(self.milk, "5")
        draft = self.upload("date,item,quantity\n2026-11-09,Latte,1\n").json()["id"]
        url = org_url("sales-import-rematch", self.org_a, pk=draft)

        self.authenticate(self.staff_a)
        self.assertEqual(self.client.post(url).status_code, 403)
        self.authenticate(self.owner_b)
        self.assertEqual(self.client.post(url).status_code, 404)
        self.apply(draft)
        self.assertEqual(self.client.post(url).status_code, 409)
        self.assertEqual(self.client.get(org_url("sales-import-detail", self.org_a, pk=draft)).json()["rematch_available"], 0)


class PreviewTests(SalesTestCase):
    def test_converts_units_and_rounds_once_per_ingredient(self):
        RecipeLine.objects.filter(menu_item=self.pizza).update(quantity=Decimal("0.4"), unit="g")
        add_movement(self.flour, "10")

        # 3 × 0.4 g = 1.2 g = 0.0012 kg, rounded once to 0.001 kg (not 3 × 0.000 kg).
        body = self.upload("date,item,quantity\n2026-11-09,Margherita Pizza,1\n2026-11-09,Margherita Pizza,2\n").json()

        self.assertEqual(body["deductions"][0]["required"], "0.001")

    def test_items_without_a_recipe_are_sold_but_deduct_nothing(self):
        body = self.upload("date,item,quantity\n2026-11-09,Water,5\n").json()

        self.assertEqual(body["items"], [{"menu_item": {"id": str(self.water.pk), "name": "Water", "is_active": True}, "quantity": "5.000", "has_recipe": False}])
        self.assertEqual(body["deductions"], [])
        self.assertIs(body["can_apply"], True)

    def test_shortfall_warning(self):
        add_movement(self.flour, "0.5")

        body = self.upload("date,item,quantity\n2026-11-09,Margherita Pizza,4\n").json()

        self.assertEqual(self.warning_codes(body), ["shortfalls"])
        self.assertEqual(body["warnings"][0]["details"][0]["shortfall"], "0.500")
        self.assertIn("never below", body["warnings"][0]["message"])

    def test_overlapping_dates_warning(self):
        add_movement(self.milk, "10")
        first = self.upload("date,item,quantity\n2026-11-09,Latte,1\n").json()["id"]
        self.apply(first)

        body = self.upload("date,item,quantity\n2026-11-09,Latte,2\n2026-11-10,Latte,1\n").json()

        self.assertEqual(self.warning_codes(body), ["overlapping_dates"])
        self.assertEqual(body["warnings"][0]["details"], ["2026-11-09"])

    def test_blocking_problems(self):
        add_movement(self.flour, "5")
        self.flour.is_active = False
        self.flour.save()

        body = self.upload("date,item,quantity\n2026-11-09,Margherita Pizza,1\n").json()

        self.assertEqual([b["code"] for b in body["blocking"]], ["archived_ingredients"])
        self.assertIs(body["can_apply"], False)

        nothing = self.upload("date,item,quantity\nbad,Latte,1\n").json()
        self.assertEqual([b["code"] for b in nothing["blocking"]], ["nothing_to_apply"])


class CountedAfterSalesTests(SalesTestCase):
    """
    A count after a sale may already include what the sale used. A sale's
    moment is its sold_at time if given, otherwise the start of its business
    date in the organization's time zone.
    """

    def count_at(self, moment, ingredient=None):
        with at(moment):
            add_movement(ingredient or self.milk, "5")
            add_movement(ingredient or self.milk, "4", kind=MovementKind.COUNT)

    def flagged(self, csv_text):
        body = self.upload(csv_text).json()
        return "counted_after_sales" in self.warning_codes(body), body

    def test_timed_sales_compare_with_their_time(self):
        self.count_at(datetime(2026, 11, 10, 11, 0, tzinfo=UTC))

        before, _ = self.flagged("date,item,quantity\n2026-11-10T12:00:00Z,Latte,1\n")
        after, body = self.flagged("date,item,quantity\n2026-11-10T10:00:00Z,Latte,1\n")

        self.assertFalse(before)  # counted at 11:00, sold at 12:00: the count cannot include it
        self.assertTrue(after)  # sold at 10:00, counted at 11:00: it may
        warning = next(w for w in body["warnings"] if w["code"] == "counted_after_sales")
        self.assertIn("count that usage twice", warning["message"])
        self.assertEqual(warning["details"][0]["ingredient"]["name"], "Milk")
        self.assertEqual(warning["details"][0]["earliest_sale"], "2026-11-10T10:00:00Z")

    def test_dated_sales_start_at_the_beginning_of_their_business_date(self):
        self.count_at(datetime(2026, 11, 10, 8, 0, tzinfo=UTC))

        same_day, _ = self.flagged("date,item,quantity\n2026-11-10,Latte,1\n")
        next_day, _ = self.flagged("date,item,quantity\n2026-11-11,Latte,1\n")

        self.assertTrue(same_day)  # the sale could have been at any time that day
        self.assertFalse(next_day)

    def test_business_date_starts_at_local_midnight(self):
        self.org_a.time_zone = "Asia/Kolkata"
        self.org_a.save()
        # 18:00 UTC is 23:30 on 10 November in India; 19:00 UTC is 00:30 on the 11th.
        self.count_at(datetime(2026, 11, 10, 18, 0, tzinfo=UTC))
        before_midnight, _ = self.flagged("date,item,quantity\n2026-11-11,Latte,1\n")

        self.count_at(datetime(2026, 11, 10, 19, 0, tzinfo=UTC), ingredient=self.flour)
        after_midnight, _ = self.flagged("date,item,quantity\n2026-11-11,Margherita Pizza,1\n")

        self.assertFalse(before_midnight)
        self.assertTrue(after_midnight)

    def test_only_counts_matter(self):
        with at(datetime(2026, 11, 10, 15, 0, tzinfo=UTC)):
            add_movement(self.milk, "5")  # a delivery, not a count

        flagged, _ = self.flagged("date,item,quantity\n2026-11-10,Latte,1\n")

        self.assertFalse(flagged)


class ApplyTests(SalesTestCase):
    def test_apply_deducts_earliest_expiry_first(self):
        late = add_movement(self.flour, "1", expires_on=date(2026, 11, 30)).created_batches.get()
        soon = add_movement(self.flour, "1", expires_on=date(2026, 11, 12)).created_batches.get()
        add_movement(self.milk, "5")
        draft = self.upload(GOOD_CSV).json()["id"]

        response = self.apply(draft)

        self.assertEqual(response.status_code, 200)
        self.assertIs(response.json()["applied_now"], True)
        self.assertEqual(response.json()["status"], "applied")
        soon.refresh_from_db()
        late.refresh_from_db()
        # 6 pizzas × 250 g = 1.5 kg: the soon-expiring batch first, then half of the later one.
        self.assertEqual((soon.quantity_remaining, late.quantity_remaining), (Decimal("0"), Decimal("0.5")))
        sale = StockMovement.objects.get(ingredient=self.flour, kind=MovementKind.SALE)
        self.assertEqual(sale.quantity_change, Decimal("-1.5"))
        self.assertEqual(sale.request_id, uuid.uuid5(DEDUCTION_NAMESPACE, f"{draft}:{self.flour.pk}"))
        self.assertEqual(sale.created_by, self.manager_a)
        deductions = {d.ingredient.name: (d.required, d.taken, d.shortfall) for d in ImportDeduction.objects.all()}
        self.assertEqual(
            deductions,
            {"Flour": (Decimal("1.5"), Decimal("1.5"), Decimal("0")), "Milk": (Decimal("2"), Decimal("2"), Decimal("0"))},
        )
        for ingredient in (self.flour, self.milk):
            ingredient.refresh_from_db()
            self.assertEqual(stock_problems(ingredient), [])
        imported = SalesImport.objects.get()
        self.assertEqual((imported.applied_by, imported.applied_at), (self.manager_a, NOW))

    def test_warnings_must_be_acknowledged(self):
        add_movement(self.flour, "0.5")
        add_movement(self.milk, "10")
        draft = self.upload(GOOD_CSV).json()["id"]

        refused = self.apply(draft)
        accepted = self.apply(draft, acknowledged=["shortfalls"])

        self.assertEqual(refused.status_code, 409)
        self.assertEqual(refused.json()["missing_acknowledgements"], ["shortfalls"])
        self.assertEqual(accepted.status_code, 200)

    def test_a_warning_that_appears_after_the_preview_must_be_acknowledged_too(self):
        add_movement(self.flour, "10")
        add_movement(self.milk, "10")
        draft = self.upload(GOOD_CSV).json()
        self.assertEqual(draft["warnings"], [])
        add_movement(self.flour, "-9.5", kind=MovementKind.COUNT)

        response = self.apply(draft["id"])

        self.assertEqual(response.status_code, 409)
        self.assertIn("shortfalls", response.json()["missing_acknowledgements"])
        self.assertIn("counted_after_sales", response.json()["missing_acknowledgements"])
        self.assertEqual(SalesImport.objects.get().status, ImportStatus.DRAFT)

    def test_shortfall_takes_stock_to_zero_and_records_the_rest(self):
        add_movement(self.flour, "0.5")
        draft = self.upload("date,item,quantity\n2026-11-09,Margherita Pizza,4\n2026-11-09,Latte,1\n").json()["id"]

        self.apply(draft, acknowledged=["shortfalls"])

        self.flour.refresh_from_db()
        self.milk.refresh_from_db()
        self.assertEqual(self.flour.quantity_on_hand, 0)
        flour = ImportDeduction.objects.get(ingredient=self.flour)
        milk = ImportDeduction.objects.get(ingredient=self.milk)
        self.assertEqual((flour.required, flour.taken, flour.shortfall), (Decimal("1"), Decimal("0.5"), Decimal("0.5")))
        # Milk had no stock at all: no stock change, the whole amount is a shortfall.
        self.assertEqual((milk.taken, milk.shortfall, milk.movement), (Decimal("0"), Decimal("0.2"), None))
        self.assertFalse(StockMovement.objects.filter(ingredient=self.milk).exists())
        self.assertEqual(stock_problems(self.flour), [])
        # The sales are recorded regardless.
        self.assertEqual(SaleLine.objects.filter(sales_import_id=draft, menu_item=self.latte).count(), 1)

    def test_applying_twice_deducts_once(self):
        add_movement(self.flour, "10")
        add_movement(self.milk, "10")
        draft = self.upload(GOOD_CSV).json()["id"]

        first = self.apply(draft)
        again = self.apply(draft)

        self.assertIs(first.json()["applied_now"], True)
        self.assertEqual(again.status_code, 200)
        self.assertIs(again.json()["applied_now"], False)
        self.assertEqual(StockMovement.objects.filter(kind=MovementKind.SALE).count(), 2)
        self.flour.refresh_from_db()
        self.assertEqual(self.flour.quantity_on_hand, Decimal("8.5"))

    def test_blocked_imports_are_refused(self):
        draft = self.upload("date,item,quantity\n2026-11-09,Mystery,1\n").json()["id"]

        response = self.apply(draft)

        self.assertEqual(response.status_code, 400)
        self.assertEqual(SalesImport.objects.get().status, ImportStatus.DRAFT)

    def test_discarded_and_applied_imports(self):
        add_movement(self.milk, "5")
        discarded = self.upload("date,item,quantity\n2026-11-09,Latte,1\n").json()["id"]
        self.client.post(org_url("sales-import-discard", self.org_a, pk=discarded))
        self.assertEqual(self.apply(discarded).status_code, 409)

        applied = self.upload("date,item,quantity\n2026-11-08,Latte,1\n").json()["id"]
        self.apply(applied)
        response = self.client.post(org_url("sales-import-discard", self.org_a, pk=applied))
        self.assertEqual(response.status_code, 409)

    def test_roles_and_tenants(self):
        add_movement(self.milk, "5")
        draft = self.upload("date,item,quantity\n2026-11-09,Latte,1\n").json()["id"]

        self.assertEqual(self.apply(draft, user=self.staff_a).status_code, 403)
        self.assertEqual(self.apply(draft, user=self.owner_b).status_code, 404)
        self.authenticate(self.owner_b)
        for name in ("sales-import-detail", "sales-import-rows"):
            self.assertEqual(self.client.get(org_url(name, self.org_a, pk=draft)).status_code, 404)
        foreign = self.client.post(org_url("sales-import-apply", self.org_b, pk=draft), {}, format="json")
        self.assertEqual(foreign.status_code, 404)
        self.authenticate(self.staff_a)
        self.assertEqual(self.client.get(org_url("sales-import-detail", self.org_a, pk=draft)).status_code, 200)
        self.assertEqual(SalesImport.objects.get().status, ImportStatus.DRAFT)

    def test_failure_midway_leaves_nothing_applied(self):
        flour_batch = add_movement(self.flour, "10").created_batches.get()
        add_movement(self.milk, "10")
        draft = self.upload(GOOD_CSV).json()["id"]
        real = services.record_stock_change
        calls = []

        def fail_on_second(*args, **kwargs):
            calls.append(kwargs["kind"])
            if len(calls) == 2:
                raise RuntimeError("database went away")
            return real(*args, **kwargs)

        with mock.patch("apps.sales.services.record_stock_change", side_effect=fail_on_second):
            with self.assertRaises(RuntimeError):
                apply_import(self.org_a, draft, self.manager_a, set())

        self.assertEqual(len(calls), 2)  # the first ingredient was deducted before the failure
        self.assertEqual(SalesImport.objects.get().status, ImportStatus.DRAFT)
        self.assertFalse(StockMovement.objects.filter(kind=MovementKind.SALE).exists())
        self.assertFalse(ImportDeduction.objects.exists())
        flour_batch.refresh_from_db()
        self.assertEqual(flour_batch.quantity_remaining, Decimal("10"))
        for ingredient in (self.flour, self.milk):
            ingredient.refresh_from_db()
            self.assertEqual(ingredient.quantity_on_hand, Decimal("10"))
            self.assertEqual(stock_problems(ingredient), [])

        # Once the problem is gone, the same import applies normally.
        self.assertEqual(self.apply(draft).status_code, 200)


class DailySalesTests(SalesTestCase):
    def test_reports_applied_sales_per_business_date(self):
        add_movement(self.flour, "10")
        add_movement(self.milk, "10")
        self.apply(self.upload(GOOD_CSV).json()["id"])
        self.upload("date,item,quantity\n2026-11-10,Latte,99\n")  # a draft: not counted
        self.authenticate(self.staff_a)

        body = self.client.get(org_url("sales-daily", self.org_a)).json()

        self.assertEqual((body["from"], body["to"]), ("2026-11-04", "2026-11-10"))
        self.assertEqual(
            [(row["business_date"], row["menu_item"]["name"], row["quantity"]) for row in body["results"]],
            [("2026-11-10", "Margherita Pizza", "2.000"), ("2026-11-09", "Latte", "10.000"), ("2026-11-09", "Margherita Pizza", "4.000")],
        )

    def test_default_range_ends_on_the_kitchens_today(self):
        self.org_a.time_zone = "Asia/Kolkata"
        self.org_a.save()
        self.authenticate(self.staff_a)

        body = self.client.get(org_url("sales-daily", self.org_a)).json()

        self.assertEqual(body["to"], "2026-11-11")

    def test_validates_the_range_and_tenant(self):
        self.authenticate(self.staff_a)
        url = org_url("sales-daily", self.org_a)
        for params in ({"from": "nope"}, {"from": "2026-11-10", "to": "2026-11-01"}, {"from": "2026-01-01", "to": "2026-11-01"}):
            with self.subTest(params=params):
                self.assertEqual(self.client.get(url, params).status_code, 400)
        self.authenticate(self.owner_b)
        self.assertEqual(self.client.get(url).status_code, 404)


class AdminTests(TestCase):
    def test_imports_are_view_only(self):
        admin_user = User.objects.create_superuser(email="admin@example.com", password=PASSWORD)
        organization = Organization.objects.create(name="Bakery")
        sales_import = SalesImport.objects.create(organization=organization, source="csv", source_reference="x")
        self.client.force_login(admin_user)
        change = reverse("admin:sales_salesimport_change", args=[sales_import.pk])

        self.assertEqual(self.client.get(change, secure=True).status_code, 200)
        self.assertEqual(self.client.post(change, {"status": "applied"}, secure=True).status_code, 403)
        sales_import.refresh_from_db()
        self.assertEqual(sales_import.status, ImportStatus.DRAFT)


class ConcurrentApplyTests(TransactionTestCase):
    """Parallel requests, each thread with its own PostgreSQL connection."""

    def setUp(self):
        self.assertEqual(connection.vendor, "postgresql")
        self.organization = Organization.objects.create(name="Bakery")
        self.manager = make_user("manager@example.com")
        Membership.objects.create(user=self.manager, organization=self.organization, role=Role.MANAGER)
        self.milk = Ingredient.objects.create(organization=self.organization, name="Milk", unit=Unit.LITRE)
        latte = MenuItem.objects.create(organization=self.organization, name="Latte")
        RecipeLine.objects.create(menu_item=latte, ingredient=self.milk, quantity=Decimal("0.2"), unit="l")
        add_movement(self.milk, "20", expires_on=date.today() + timedelta(days=30))

    def draft(self, text):
        return services_draft(self.organization, self.manager, text)

    def run_together(self, *requests):
        barrier = threading.Barrier(len(requests))
        results = [None] * len(requests)

        def send(index, method, url, data):
            client = HttpsAPIClient()
            client.credentials(HTTP_AUTHORIZATION=f"Bearer {RefreshToken.for_user(self.manager).access_token}")
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

    def apply_url(self, sales_import):
        return org_url("sales-import-apply", self.organization, pk=sales_import.pk)

    def test_simultaneous_applies_deduct_once(self):
        sales_import = self.draft("date,item,quantity\n2026-11-02,Latte,10\n")

        results = self.run_together(*[("post", self.apply_url(sales_import), {"acknowledged": []})] * 3)

        self.assertEqual([status for status, _ in results], [200] * 3)
        self.assertEqual(sorted(body["applied_now"] for _, body in results), [False, False, True])
        self.milk.refresh_from_db()
        self.assertEqual(self.milk.quantity_on_hand, Decimal("18"))
        self.assertEqual(stock_problems(self.milk), [])

    def test_overlapping_imports_cannot_both_skip_the_warning(self):
        first = self.draft("date,item,quantity\n2026-11-02,Latte,1\n")
        second = self.draft("date,item,quantity\n2026-11-02,Latte,2\n")

        results = self.run_together(
            ("post", self.apply_url(first), {"acknowledged": []}),
            ("post", self.apply_url(second), {"acknowledged": []}),
        )

        statuses = sorted(status for status, _ in results)
        self.assertEqual(statuses, [200, 409])
        refused = next(body for status, body in results if status == 409)
        self.assertEqual(refused["missing_acknowledgements"], ["overlapping_dates"])

    def test_apply_during_deliveries_keeps_stock_consistent(self):
        sales_import = self.draft("date,item,quantity\n2026-11-02,Latte,10\n")
        receive = org_url("ingredient-receive", self.organization, pk=self.milk.pk)

        results = self.run_together(
            ("post", receive, receive_data("1")),
            ("post", self.apply_url(sales_import), {"acknowledged": []}),
            ("post", receive, receive_data("2")),
        )

        self.assertEqual(sorted(status for status, _ in results), [200, 201, 201])
        self.milk.refresh_from_db()
        self.assertEqual(self.milk.quantity_on_hand, Decimal("21"))
        self.assertEqual(stock_problems(self.milk), [])


def services_draft(organization, user, text):
    """Creates a draft import the way the upload endpoint does."""
    client = HttpsAPIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {RefreshToken.for_user(user).access_token}")
    response = client.post(
        org_url("sales-import-list", organization), {"file": csv_upload(text)}, format="multipart"
    )
    assert response.status_code == 201, response.content
    return SalesImport.objects.get(pk=response.json()["id"])
