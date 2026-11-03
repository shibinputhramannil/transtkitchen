import threading
import uuid
from datetime import timedelta
from decimal import Decimal

from django.contrib import admin
from django.db import IntegrityError, connection, transaction
from django.test import RequestFactory, TestCase, TransactionTestCase
from django.urls import reverse
from rest_framework_simplejwt.tokens import RefreshToken

from apps.accounts.models import User
from apps.core.testing import HttpsAPIClient
from apps.organizations.models import Membership, Organization, Role
from apps.organizations.testing import PASSWORD, TenantTestCase, make_user, org_url

from .models import Ingredient, MovementKind, StockMovement, Unit
from .stock import record_stock_change, stock_problems


def ingredient_url(name, ingredient, organization=None):
    return org_url(name, organization or ingredient.organization, pk=ingredient.pk)


def receive_data(quantity, **extra):
    return {"quantity": str(quantity), "request_id": str(uuid.uuid4()), **extra}


def count_data(counted, **extra):
    return {"counted_quantity": str(counted), "request_id": str(uuid.uuid4()), **extra}


def add_movement(ingredient, change, kind=MovementKind.RECEIVE, expires_on=None):
    """Records a stock change the way the API does, batches included."""
    with transaction.atomic():
        locked = Ingredient.objects.select_for_update().get(pk=ingredient.pk)
        movement = record_stock_change(
            locked, kind=kind, change=Decimal(change), request_id=uuid.uuid4(), expires_on=expires_on
        )
    ingredient.refresh_from_db()
    return movement


class InventoryTestCase(TenantTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.flour = Ingredient.objects.create(
            organization=cls.org_a, name="Flour", unit=Unit.KILOGRAM, reorder_level=Decimal("5")
        )
        cls.milk_b = Ingredient.objects.create(organization=cls.org_b, name="Milk", unit=Unit.LITRE)

    def receive(self, ingredient, data, organization=None):
        return self.client.post(ingredient_url("ingredient-receive", ingredient, organization), data)

    def count(self, ingredient, data, organization=None):
        return self.client.post(ingredient_url("ingredient-count", ingredient, organization), data)


class ModelTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Bakery")
        self.flour = Ingredient.objects.create(
            organization=self.organization, name="Flour", unit=Unit.KILOGRAM
        )

    def test_names_are_unique_per_organization_ignoring_case(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            Ingredient.objects.create(organization=self.organization, name="FLOUR", unit=Unit.GRAM)

        other = Organization.objects.create(name="Other kitchen")
        Ingredient.objects.create(organization=other, name="Flour", unit=Unit.GRAM)

    def test_database_rejects_negative_stock(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            Ingredient.objects.filter(pk=self.flour.pk).update(quantity_on_hand=Decimal("-0.001"))

    def test_database_rejects_unknown_units(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            Ingredient.objects.create(organization=self.organization, name="Sugar", unit="cup")

    def test_only_counts_may_record_no_change(self):
        StockMovement.objects.create(
            ingredient=self.flour, kind=MovementKind.COUNT, quantity_change=0,
            quantity_after=0, request_id=uuid.uuid4(),
        )

        with self.assertRaises(IntegrityError), transaction.atomic():
            StockMovement.objects.create(
                ingredient=self.flour, kind=MovementKind.RECEIVE, quantity_change=0,
                quantity_after=0, request_id=uuid.uuid4(),
            )

    def test_movements_cannot_be_changed(self):
        movement = add_movement(self.flour, "2")
        movement.note = "edited"

        with self.assertRaisesMessage(ValueError, "Stock movements cannot be changed."):
            movement.save()

    def test_ingredient_with_history_cannot_be_deleted(self):
        add_movement(self.flour, "2")

        with self.assertRaises(Exception), transaction.atomic():
            self.flour.delete()

    def test_low_stock(self):
        self.assertFalse(self.flour.is_low_stock)
        self.flour.reorder_level = Decimal("1")
        self.assertTrue(self.flour.is_low_stock)


class IngredientApiTests(InventoryTestCase):
    list_url_a = property(lambda self: org_url("ingredient-list", self.org_a))

    def test_requires_authentication(self):
        self.assertEqual(self.client.get(self.list_url_a).status_code, 401)

    def test_members_see_only_their_organizations_ingredients(self):
        self.authenticate(self.staff_a)
        names_a = [item["name"] for item in self.client.get(self.list_url_a).json()["results"]]

        self.authenticate(self.owner_b)
        names_b = [
            item["name"]
            for item in self.client.get(org_url("ingredient-list", self.org_b)).json()["results"]
        ]

        self.assertEqual(names_a, ["Flour"])
        self.assertEqual(names_b, ["Milk"])

    def test_other_tenants_and_outsiders_get_404(self):
        for user in (self.owner_b, self.outsider):
            with self.subTest(user=user.email):
                self.authenticate(user)
                self.assertEqual(self.client.get(self.list_url_a).status_code, 404)
                self.assertEqual(
                    self.client.get(ingredient_url("ingredient-detail", self.flour)).status_code, 404
                )

    def test_only_managers_and_owners_create(self):
        for user, name, expected in [
            (self.staff_a, "Sugar", 403),
            (self.manager_a, "Sugar", 201),
            (self.owner_a, "Butter", 201),
        ]:
            with self.subTest(user=user.email):
                self.authenticate(user)
                response = self.client.post(self.list_url_a, {"name": name, "unit": "kg"})
                self.assertEqual(response.status_code, expected)

    def test_create_takes_organization_from_the_url_and_starts_at_zero(self):
        self.authenticate(self.owner_a)

        response = self.client.post(
            self.list_url_a,
            {"name": "Sugar", "unit": "g", "organization": str(self.org_b.pk), "quantity_on_hand": "50"},
        )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["quantity_on_hand"], "0.000")
        sugar = Ingredient.objects.get(name="Sugar")
        self.assertEqual(sugar.organization, self.org_a)

    def test_rejects_duplicate_name_in_same_organization_only(self):
        self.authenticate(self.owner_a)
        self.assertEqual(self.client.post(self.list_url_a, {"name": "flour", "unit": "g"}).status_code, 400)

        self.authenticate(self.owner_b)
        response = self.client.post(org_url("ingredient-list", self.org_b), {"name": "Flour", "unit": "g"})
        self.assertEqual(response.status_code, 201)

    def test_validates_unit_and_reorder_level(self):
        self.authenticate(self.owner_a)

        self.assertEqual(self.client.post(self.list_url_a, {"name": "Sugar", "unit": "cup"}).status_code, 400)
        self.assertEqual(
            self.client.post(self.list_url_a, {"name": "Sugar", "unit": "g", "reorder_level": "-1"}).status_code,
            400,
        )

    def test_only_managers_and_owners_edit(self):
        url = ingredient_url("ingredient-detail", self.flour)
        for user, expected in [(self.staff_a, 403), (self.manager_a, 200), (self.owner_b, 404)]:
            with self.subTest(user=user.email):
                self.authenticate(user)
                self.assertEqual(self.client.patch(url, {"reorder_level": "2"}).status_code, expected)

    def test_cannot_reach_another_tenants_ingredient_through_own_organization(self):
        self.authenticate(self.owner_a)
        url = ingredient_url("ingredient-detail", self.milk_b, organization=self.org_a)

        self.assertEqual(self.client.get(url).status_code, 404)
        self.assertEqual(self.client.patch(url, {"name": "Taken"}).status_code, 404)
        self.assertEqual(self.client.delete(url).status_code, 404)
        self.milk_b.refresh_from_db()
        self.assertEqual(self.milk_b.name, "Milk")

    def test_stock_level_cannot_be_edited_directly(self):
        self.authenticate(self.owner_a)

        self.client.patch(ingredient_url("ingredient-detail", self.flour), {"quantity_on_hand": "99"})

        self.flour.refresh_from_db()
        self.assertEqual(self.flour.quantity_on_hand, 0)

    def test_unit_changes_only_before_stock_history(self):
        self.authenticate(self.owner_a)
        url = ingredient_url("ingredient-detail", self.flour)

        self.assertEqual(self.client.patch(url, {"unit": "g"}).status_code, 200)
        self.flour.refresh_from_db()
        add_movement(self.flour, "1")
        response = self.client.patch(url, {"unit": "kg"})

        self.assertEqual(response.status_code, 400)
        self.assertIn("unit", response.json())
        self.flour.refresh_from_db()
        self.assertEqual(self.flour.unit, Unit.GRAM)

    def test_delete_only_without_stock_history(self):
        sugar = Ingredient.objects.create(organization=self.org_a, name="Sugar", unit=Unit.GRAM)
        add_movement(self.flour, "1")
        self.authenticate(self.manager_a)

        self.assertEqual(self.client.delete(ingredient_url("ingredient-detail", sugar)).status_code, 204)
        self.assertEqual(self.client.delete(ingredient_url("ingredient-detail", self.flour)).status_code, 400)
        self.assertTrue(Ingredient.objects.filter(pk=self.flour.pk).exists())

    def test_archived_ingredients_are_listed_separately(self):
        Ingredient.objects.create(organization=self.org_a, name="Old Yeast", unit=Unit.GRAM, is_active=False)
        self.authenticate(self.staff_a)

        active = self.client.get(self.list_url_a).json()["results"]
        archived = self.client.get(self.list_url_a, {"archived": "true"}).json()["results"]

        self.assertEqual([item["name"] for item in active], ["Flour"])
        self.assertEqual([item["name"] for item in archived], ["Old Yeast"])

    def test_search_matches_part_of_the_name(self):
        Ingredient.objects.create(organization=self.org_a, name="Brown Sugar", unit=Unit.GRAM)
        Ingredient.objects.create(organization=self.org_a, name="Icing sugar", unit=Unit.GRAM)
        self.authenticate(self.staff_a)

        results = self.client.get(self.list_url_a, {"search": "SUGAR"}).json()["results"]

        self.assertEqual([item["name"] for item in results], ["Brown Sugar", "Icing sugar"])

    def test_list_is_paginated(self):
        Ingredient.objects.bulk_create(
            Ingredient(organization=self.org_a, name=f"Spice {number:02}", unit=Unit.GRAM)
            for number in range(55)
        )
        self.authenticate(self.staff_a)

        page = self.client.get(self.list_url_a).json()

        self.assertEqual(page["count"], 56)
        self.assertEqual(len(page["results"]), 50)
        self.assertIsNotNone(page["next"])

    def test_reports_low_stock(self):
        self.authenticate(self.staff_a)

        item = self.client.get(ingredient_url("ingredient-detail", self.flour)).json()

        self.assertEqual(item["quantity_on_hand"], "0.000")
        self.assertEqual(item["reorder_level"], "5.000")
        self.assertIs(item["is_low_stock"], True)


class StockChangeTests(InventoryTestCase):
    def test_any_member_records_a_delivery(self):
        self.authenticate(self.staff_a)

        response = self.receive(self.flour, receive_data("12.5", note="Mill Co."))

        self.assertEqual(response.status_code, 201)
        movement = response.json()
        self.assertEqual(movement["kind"], "receive")
        self.assertEqual(movement["quantity_change"], "12.500")
        self.assertEqual(movement["quantity_after"], "12.500")
        self.assertEqual(movement["note"], "Mill Co.")
        self.assertEqual(movement["created_by"], "staff@a.example")
        self.flour.refresh_from_db()
        self.assertEqual(self.flour.quantity_on_hand, Decimal("12.5"))

    def test_delivery_quantity_must_be_a_positive_decimal(self):
        self.authenticate(self.staff_a)
        for value in ["0", "-1", "abc", "NaN", "Infinity", "1.0001", "1000000000"]:
            with self.subTest(value=value):
                self.assertEqual(self.receive(self.flour, receive_data(value)).status_code, 400)

        self.flour.refresh_from_db()
        self.assertEqual(self.flour.quantity_on_hand, 0)
        self.assertFalse(self.flour.movements.exists())

    def test_request_id_is_required(self):
        self.authenticate(self.staff_a)

        self.assertEqual(self.receive(self.flour, {"quantity": "1"}).status_code, 400)
        self.assertEqual(self.receive(self.flour, {"quantity": "1", "request_id": "nope"}).status_code, 400)

    def test_stock_cannot_exceed_the_maximum(self):
        self.authenticate(self.staff_a)
        self.receive(self.flour, receive_data("999999999.999"))

        self.assertEqual(self.receive(self.flour, receive_data("1")).status_code, 400)

    def test_count_sets_stock_and_records_the_difference(self):
        add_movement(self.flour, "10")
        self.authenticate(self.staff_a)

        response = self.count(self.flour, count_data("7.5", expected_quantity="10"))

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["quantity_change"], "-2.500")
        self.assertEqual(response.json()["quantity_after"], "7.500")
        self.flour.refresh_from_db()
        self.assertEqual(self.flour.quantity_on_hand, Decimal("7.5"))

    def test_count_that_confirms_stock_is_recorded(self):
        add_movement(self.flour, "3")
        self.authenticate(self.staff_a)

        response = self.count(self.flour, count_data("3"))

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["quantity_change"], "0.000")

    def test_stale_count_is_refused(self):
        add_movement(self.flour, "10")
        self.authenticate(self.staff_a)

        # The counter started at 0, but a delivery of 10 arrived meanwhile.
        response = self.count(self.flour, count_data("4", expected_quantity="0"))

        self.assertEqual(response.status_code, 409)
        self.flour.refresh_from_db()
        self.assertEqual(self.flour.quantity_on_hand, Decimal("10"))
        self.assertEqual(self.flour.movements.count(), 1)

    def test_count_cannot_be_negative(self):
        self.authenticate(self.staff_a)

        self.assertEqual(self.count(self.flour, count_data("-1")).status_code, 400)

    def test_repeated_submission_changes_stock_once(self):
        self.authenticate(self.staff_a)
        data = receive_data("2")

        first = self.receive(self.flour, data)
        retry = self.receive(self.flour, data)

        self.assertEqual(first.status_code, 201)
        self.assertEqual(retry.status_code, 200)
        self.assertEqual(retry.json(), first.json())
        self.flour.refresh_from_db()
        self.assertEqual(self.flour.quantity_on_hand, Decimal("2"))
        self.assertEqual(self.flour.movements.count(), 1)

    def test_repeated_count_after_stock_moved_on_is_still_a_replay(self):
        self.authenticate(self.staff_a)
        data = count_data("5", expected_quantity="0")
        self.count(self.flour, data)

        # The expected quantity no longer matches, but this is the same submission.
        self.assertEqual(self.count(self.flour, data).status_code, 200)

    def test_request_id_reused_for_a_different_change_is_refused(self):
        self.authenticate(self.staff_a)
        data = receive_data("2")
        self.receive(self.flour, data)

        different_amount = self.receive(self.flour, {**data, "quantity": "3"})
        different_kind = self.count(self.flour, count_data("2", request_id=data["request_id"]))

        self.assertEqual(different_amount.status_code, 409)
        self.assertEqual(different_kind.status_code, 409)
        self.flour.refresh_from_db()
        self.assertEqual(self.flour.quantity_on_hand, Decimal("2"))

    def test_archived_ingredients_cannot_change_stock(self):
        self.flour.is_active = False
        self.flour.save()
        self.authenticate(self.staff_a)

        self.assertEqual(self.receive(self.flour, receive_data("1")).status_code, 400)
        self.assertEqual(self.count(self.flour, count_data("1")).status_code, 400)

    def test_cannot_change_another_tenants_stock(self):
        self.authenticate(self.owner_a)

        through_own = self.receive(self.milk_b, receive_data("5"), organization=self.org_a)
        directly = self.receive(self.milk_b, receive_data("5"))
        count_through_own = self.count(self.milk_b, count_data("5"), organization=self.org_a)

        self.assertEqual(through_own.status_code, 404)
        self.assertEqual(directly.status_code, 404)
        self.assertEqual(count_through_own.status_code, 404)
        self.milk_b.refresh_from_db()
        self.assertEqual(self.milk_b.quantity_on_hand, 0)
        self.assertFalse(self.milk_b.movements.exists())


class StockHistoryTests(InventoryTestCase):
    def test_lists_movements_newest_first(self):
        add_movement(self.flour, "4")
        add_movement(self.flour, "-1", kind=MovementKind.COUNT)
        self.authenticate(self.staff_a)

        results = self.client.get(ingredient_url("ingredient-movements", self.flour)).json()["results"]

        self.assertEqual(
            [(item["kind"], item["quantity_after"]) for item in results],
            [("count", "3.000"), ("receive", "4.000")],
        )

    def test_history_is_read_only(self):
        add_movement(self.flour, "4")
        self.authenticate(self.owner_a)
        url = ingredient_url("ingredient-movements", self.flour)

        for method in ("post", "put", "patch", "delete"):
            with self.subTest(method=method):
                self.assertEqual(getattr(self.client, method)(url, {}).status_code, 405)
        self.assertEqual(self.flour.movements.count(), 1)

    def test_cannot_read_another_tenants_history(self):
        add_movement(self.milk_b, "4")
        self.authenticate(self.owner_a)

        self.assertEqual(
            self.client.get(
                ingredient_url("ingredient-movements", self.milk_b, organization=self.org_a)
            ).status_code,
            404,
        )


class AdminTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin_user = User.objects.create_superuser(email="admin@example.com", password=PASSWORD)
        organization = Organization.objects.create(name="Bakery")
        cls.flour = Ingredient.objects.create(organization=organization, name="Flour", unit=Unit.KILOGRAM)
        cls.movement = add_movement(cls.flour, "2")

    def setUp(self):
        self.client.force_login(self.admin_user)

    def test_movements_are_view_only(self):
        change_url = reverse("admin:inventory_stockmovement_change", args=[self.movement.pk])

        self.assertEqual(self.client.get(change_url, secure=True).status_code, 200)
        self.assertEqual(self.client.post(change_url, {"note": "edited"}, secure=True).status_code, 403)
        self.assertEqual(
            self.client.post(
                reverse("admin:inventory_stockmovement_delete", args=[self.movement.pk]),
                {"post": "yes"},
                secure=True,
            ).status_code,
            403,
        )
        self.assertEqual(
            self.client.get(reverse("admin:inventory_stockmovement_add"), secure=True).status_code, 403
        )
        self.movement.refresh_from_db()
        self.assertEqual(self.movement.note, "")

    def test_stock_and_unit_are_read_only_in_ingredient_admin(self):
        ingredient_admin = admin.site._registry[Ingredient]
        request = RequestFactory().get("/")
        request.user = self.admin_user
        new = Ingredient(name="Sugar", unit=Unit.GRAM)

        self.assertEqual(ingredient_admin.get_readonly_fields(request, new), ["quantity_on_hand"])
        self.assertEqual(
            ingredient_admin.get_readonly_fields(request, self.flour), ["quantity_on_hand", "unit"]
        )
        inline = ingredient_admin.get_inline_instances(request, self.flour)[0]
        self.assertFalse(inline.has_add_permission(request, self.flour))
        self.assertFalse(inline.has_change_permission(request, self.flour))
        self.assertFalse(inline.has_delete_permission(request, self.flour))

    def test_ingredient_admin_pages_load(self):
        self.assertEqual(
            self.client.get(
                reverse("admin:inventory_ingredient_change", args=[self.flour.pk]), secure=True
            ).status_code,
            200,
        )
        self.assertEqual(
            self.client.get(reverse("admin:inventory_ingredient_changelist"), secure=True).status_code,
            200,
        )


class ConcurrentStockChangeTests(TransactionTestCase):
    """
    Sends requests at the same moment from parallel threads. Each thread uses
    its own PostgreSQL connection, so the row lock is what keeps them apart.
    """

    def setUp(self):
        self.assertEqual(connection.vendor, "postgresql")
        organization = Organization.objects.create(name="Bakery")
        self.cook = make_user("cook@example.com")
        self.manager = make_user("manager@example.com")
        Membership.objects.create(user=self.cook, organization=organization, role=Role.STAFF)
        Membership.objects.create(user=self.manager, organization=organization, role=Role.MANAGER)
        self.flour = Ingredient.objects.create(
            organization=organization, name="Flour", unit=Unit.KILOGRAM
        )

    def run_together(self, *requests):
        """Runs each (user, method, url, data) request in its own thread; returns status codes."""
        barrier = threading.Barrier(len(requests))
        statuses = [None] * len(requests)

        def send(index, user, method, url, data):
            client = HttpsAPIClient()
            client.credentials(
                HTTP_AUTHORIZATION=f"Bearer {RefreshToken.for_user(user).access_token}"
            )
            try:
                barrier.wait(timeout=10)
                statuses[index] = getattr(client, method)(url, data, format="json").status_code
            finally:
                connection.close()

        threads = [
            threading.Thread(target=send, args=(index, *request))
            for index, request in enumerate(requests)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        return statuses

    def test_simultaneous_deliveries_are_all_applied(self):
        url = ingredient_url("ingredient-receive", self.flour)

        statuses = self.run_together(
            *[(self.cook, "post", url, receive_data("1.5")) for _ in range(8)]
        )

        self.assertEqual(statuses, [201] * 8)
        self.flour.refresh_from_db()
        self.assertEqual(self.flour.quantity_on_hand, Decimal("12"))
        # Each delivery saw the one before it: no two recorded the same level.
        self.assertEqual(
            sorted(self.flour.movements.values_list("quantity_after", flat=True)),
            [Decimal("1.5") * step for step in range(1, 9)],
        )
        self.assertEqual(self.flour.batches.count(), 8)
        self.assertEqual(stock_problems(self.flour), [])

    def test_simultaneous_deliveries_with_expiry_dates_get_their_own_batches(self):
        url = ingredient_url("ingredient-receive", self.flour)
        today = self.flour.organization.local_today()
        dates = [today + timedelta(days=offset) for offset in (5, 2, 9, 2)]

        statuses = self.run_together(
            *[
                (self.cook, "post", url, receive_data("1", expires_on=expires_on.isoformat()))
                for expires_on in dates
            ]
        )

        self.assertEqual(statuses, [201] * 4)
        self.assertEqual(
            sorted(self.flour.batches.values_list("expires_on", flat=True)), sorted(dates)
        )
        self.flour.refresh_from_db()
        self.assertEqual(stock_problems(self.flour), [])

    def test_count_during_deliveries_keeps_batches_consistent(self):
        add_movement(self.flour, "10", expires_on=self.flour.organization.local_today() + timedelta(days=3))
        receive = ingredient_url("ingredient-receive", self.flour)

        statuses = self.run_together(
            (self.cook, "post", receive, receive_data("2")),
            (self.cook, "post", ingredient_url("ingredient-count", self.flour), count_data("4")),
            (self.cook, "post", receive, receive_data("3")),
        )

        self.assertEqual(statuses, [201] * 3)
        self.flour.refresh_from_db()
        self.assertEqual(stock_problems(self.flour), [])

    def test_simultaneous_duplicate_submissions_apply_once(self):
        url = ingredient_url("ingredient-receive", self.flour)
        data = receive_data("2")

        statuses = self.run_together(*[(self.cook, "post", url, data) for _ in range(4)])

        self.assertEqual(sorted(statuses), [200, 200, 200, 201])
        self.flour.refresh_from_db()
        self.assertEqual(self.flour.quantity_on_hand, Decimal("2"))
        self.assertEqual(self.flour.movements.count(), 1)
        self.assertEqual(self.flour.batches.count(), 1)
        self.assertEqual(stock_problems(self.flour), [])

    def test_deletion_and_delivery_never_interleave(self):
        statuses = self.run_together(
            (self.manager, "delete", ingredient_url("ingredient-detail", self.flour), None),
            (self.cook, "post", ingredient_url("ingredient-receive", self.flour), receive_data("1")),
        )

        # Either the deletion went first and the delivery found nothing, or the
        # delivery went first and the deletion was refused.
        self.assertIn(statuses, [[204, 404], [400, 201]])
        if statuses == [400, 201]:
            self.flour.refresh_from_db()
            self.assertEqual(self.flour.movements.count(), 1)
        else:
            self.assertFalse(Ingredient.objects.filter(pk=self.flour.pk).exists())
