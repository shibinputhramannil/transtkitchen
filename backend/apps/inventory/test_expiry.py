import uuid
from datetime import date, datetime, timedelta
from datetime import timezone as dt_timezone
from decimal import Decimal
from unittest import mock

from django.contrib import admin
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import RequestFactory, SimpleTestCase, TestCase, TransactionTestCase
from django.urls import reverse

from apps.accounts.models import User
from apps.organizations.models import Organization
from apps.organizations.testing import PASSWORD, TenantTestCase, org_url

from .models import BatchSource, Ingredient, MovementKind, MovementLine, StockBatch, StockMovement, Unit
from .stock import expiry_status, stock_problems
from .tests import add_movement, count_data, ingredient_url, receive_data

# 20:00 UTC on 10 November is already 01:30 on 11 November in India.
NOW = datetime(2026, 11, 10, 20, 0, tzinfo=dt_timezone.utc)
TODAY_UTC = date(2026, 11, 10)
TODAY_KOLKATA = date(2026, 11, 11)


def at(moment):
    return mock.patch("django.utils.timezone.now", return_value=moment)


class ExpiryTestCase(TenantTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.milk = Ingredient.objects.create(organization=cls.org_a, name="Milk", unit=Unit.LITRE)
        cls.oil_b = Ingredient.objects.create(organization=cls.org_b, name="Oil", unit=Unit.LITRE)

    def setUp(self):
        super().setUp()
        clock = at(NOW)
        clock.start()
        self.addCleanup(clock.stop)

    def assertConsistent(self, *ingredients):
        for ingredient in ingredients:
            ingredient.refresh_from_db()
            self.assertEqual(stock_problems(ingredient), [])

    def receive(self, ingredient, quantity, expires_on=None, **extra):
        data = receive_data(quantity, **extra)
        if expires_on is not None:
            data["expires_on"] = expires_on.isoformat()
        return self.client.post(ingredient_url("ingredient-receive", ingredient), data)


class ExpiryStatusTests(SimpleTestCase):
    def test_boundaries(self):
        today = date(2026, 11, 10)
        cases = [
            (None, "no_expiry"),
            (today - timedelta(days=1), "expired"),
            (today, "expiring"),
            (today + timedelta(days=3), "expiring"),
            (today + timedelta(days=4), "ok"),
        ]
        for expires_on, expected in cases:
            with self.subTest(expires_on=expires_on):
                self.assertEqual(expiry_status(expires_on, today), expected)


class OrganizationTimeZoneTests(TenantTestCase):
    def test_defaults_to_utc(self):
        self.authenticate(self.staff_a)

        self.assertEqual(self.client.get(org_url("organization-detail", self.org_a)).json()["time_zone"], "UTC")

    def test_set_on_creation_including_browser_aliases(self):
        self.authenticate(self.outsider)
        for time_zone in ("Asia/Kolkata", "Asia/Calcutta", "America/New_York"):
            with self.subTest(time_zone=time_zone):
                response = self.client.post(
                    reverse("organization-list"), {"name": f"Kitchen {time_zone}", "time_zone": time_zone}
                )
                self.assertEqual(response.status_code, 201)
                self.assertEqual(response.json()["time_zone"], time_zone)

    def test_rejects_unknown_time_zones(self):
        self.authenticate(self.outsider)

        response = self.client.post(reverse("organization-list"), {"name": "X", "time_zone": "Mars/Base"})

        self.assertEqual(response.status_code, 400)
        self.assertIn("time_zone", response.json())

    def test_only_owners_change_it(self):
        url = org_url("organization-detail", self.org_a)
        for user, expected in [(self.staff_a, 403), (self.manager_a, 403), (self.owner_a, 200)]:
            with self.subTest(user=user.email):
                self.authenticate(user)
                self.assertEqual(self.client.patch(url, {"time_zone": "Europe/London"}).status_code, expected)

    def test_local_today_uses_the_organizations_time_zone(self):
        with at(NOW):
            self.assertEqual(Organization(time_zone="UTC").local_today(), TODAY_UTC)
            self.assertEqual(Organization(time_zone="Asia/Kolkata").local_today(), TODAY_KOLKATA)


class DeliveryBatchTests(ExpiryTestCase):
    def test_delivery_creates_a_batch_with_its_expiry_date(self):
        self.authenticate(self.staff_a)

        response = self.receive(self.milk, "6", expires_on=date(2026, 11, 14))

        self.assertEqual(response.status_code, 201)
        batch = self.milk.batches.get()
        self.assertEqual(batch.source, BatchSource.DELIVERY)
        self.assertEqual(batch.quantity_received, Decimal("6"))
        self.assertEqual(batch.quantity_remaining, Decimal("6"))
        self.assertEqual(batch.expires_on, date(2026, 11, 14))
        self.assertEqual(str(batch.source_movement_id), response.json()["id"])
        self.assertEqual(response.json()["expires_on"], "2026-11-14")
        self.assertEqual(
            response.json()["lines"],
            [{"batch": str(batch.pk), "quantity_change": "6.000", "expires_on": "2026-11-14"}],
        )
        self.assertConsistent(self.milk)

    def test_expiry_date_is_optional(self):
        self.authenticate(self.staff_a)

        response = self.client.post(
            ingredient_url("ingredient-receive", self.milk),
            {"quantity": "2", "request_id": str(uuid.uuid4())},
        )

        self.assertEqual(response.status_code, 201)
        self.assertIsNone(self.milk.batches.get().expires_on)
        self.assertConsistent(self.milk)

    def test_each_delivery_is_its_own_batch(self):
        self.authenticate(self.staff_a)

        self.receive(self.milk, "1", expires_on=date(2026, 11, 14))
        self.receive(self.milk, "1", expires_on=date(2026, 11, 14))

        self.assertEqual(self.milk.batches.count(), 2)

    def test_expiry_date_range_uses_the_organizations_local_date(self):
        self.authenticate(self.staff_a)
        cases = [
            (TODAY_UTC - timedelta(days=1), 400),
            (TODAY_UTC, 201),
            (date(2031, 11, 10), 201),
            (date(2031, 11, 11), 400),
        ]
        for expires_on, expected in cases:
            with self.subTest(expires_on=expires_on):
                self.assertEqual(self.receive(self.milk, "1", expires_on=expires_on).status_code, expected)

        # In India it is already 11 November, so 10 November is in the past.
        self.org_a.time_zone = "Asia/Kolkata"
        self.org_a.save()
        self.assertEqual(self.receive(self.milk, "1", expires_on=TODAY_UTC).status_code, 400)
        self.assertEqual(self.receive(self.milk, "1", expires_on=TODAY_KOLKATA).status_code, 201)

    def test_retry_with_the_same_expiry_is_recognised(self):
        self.authenticate(self.staff_a)
        data = receive_data("2", expires_on="2026-11-14")
        url = ingredient_url("ingredient-receive", self.milk)

        first = self.client.post(url, data)
        retry = self.client.post(url, data)
        other_date = self.client.post(url, {**data, "expires_on": "2026-11-15"})
        no_date = self.client.post(url, {"quantity": "2", "request_id": data["request_id"]})

        self.assertEqual(first.status_code, 201)
        self.assertEqual(retry.status_code, 200)
        self.assertEqual(other_date.status_code, 409)
        self.assertEqual(no_date.status_code, 409)
        self.assertEqual(self.milk.batches.count(), 1)

    def test_retry_after_the_expiry_date_has_passed_is_still_recognised(self):
        self.authenticate(self.staff_a)
        data = receive_data("2", expires_on=TODAY_UTC.isoformat())
        url = ingredient_url("ingredient-receive", self.milk)
        self.client.post(url, data)

        with at(NOW + timedelta(days=2)):
            self.assertEqual(self.client.post(url, data).status_code, 200)

    def test_rejects_invalid_dates(self):
        self.authenticate(self.staff_a)

        response = self.client.post(
            ingredient_url("ingredient-receive", self.milk), receive_data("1", expires_on="14/11/2026")
        )

        self.assertEqual(response.status_code, 400)


class CountReconciliationTests(ExpiryTestCase):
    def test_shortfall_is_taken_earliest_expiry_first(self):
        late = add_movement(self.milk, "2", expires_on=date(2026, 11, 20))
        soon_first = add_movement(self.milk, "3", expires_on=date(2026, 11, 12))
        undated = add_movement(self.milk, "4")
        soon_second = add_movement(self.milk, "1", expires_on=date(2026, 11, 12))
        self.authenticate(self.staff_a)

        response = self.client.post(ingredient_url("ingredient-count", self.milk), count_data("4"))

        self.assertEqual(response.status_code, 201)
        taken = {line["batch"]: line["quantity_change"] for line in response.json()["lines"]}
        batch = {m: m.created_batches.get() for m in (late, soon_first, undated, soon_second)}
        # Same expiry date: oldest delivery first. Undated stock is used last.
        self.assertEqual(
            taken,
            {
                str(batch[soon_first].pk): "-3.000",
                str(batch[soon_second].pk): "-1.000",
                str(batch[late].pk): "-2.000",
            },
        )
        batch[undated].refresh_from_db()
        self.assertEqual(batch[undated].quantity_remaining, Decimal("4"))
        self.assertConsistent(self.milk)

    def test_expired_stock_is_taken_first(self):
        add_movement(self.milk, "2", expires_on=date(2026, 11, 15))
        with at(NOW - timedelta(days=5)):
            expired = add_movement(self.milk, "2", expires_on=date(2026, 11, 8)).created_batches.get()
        self.authenticate(self.staff_a)

        self.client.post(ingredient_url("ingredient-count", self.milk), count_data("3"))

        expired.refresh_from_db()
        self.assertEqual(expired.quantity_remaining, Decimal("1"))
        self.assertConsistent(self.milk)

    def test_extra_stock_becomes_a_batch_without_expiry(self):
        add_movement(self.milk, "2", expires_on=date(2026, 11, 15))
        self.authenticate(self.staff_a)

        response = self.client.post(ingredient_url("ingredient-count", self.milk), count_data("5"))

        found = self.milk.batches.get(source=BatchSource.COUNT)
        self.assertEqual(found.quantity_received, Decimal("3"))
        self.assertEqual(found.quantity_remaining, Decimal("3"))
        self.assertIsNone(found.expires_on)
        self.assertEqual(str(found.source_movement_id), response.json()["id"])
        self.assertEqual(
            response.json()["lines"],
            [{"batch": str(found.pk), "quantity_change": "3.000", "expires_on": None}],
        )
        self.assertConsistent(self.milk)

    def test_matching_count_touches_no_batches(self):
        add_movement(self.milk, "2")
        self.authenticate(self.staff_a)

        response = self.client.post(ingredient_url("ingredient-count", self.milk), count_data("2"))

        self.assertEqual(response.json()["lines"], [])
        self.assertConsistent(self.milk)

    def test_count_to_zero_empties_every_batch(self):
        add_movement(self.milk, "2", expires_on=date(2026, 11, 15))
        add_movement(self.milk, "1")
        self.authenticate(self.staff_a)

        self.client.post(ingredient_url("ingredient-count", self.milk), count_data("0"))

        self.assertFalse(self.milk.batches.filter(quantity_remaining__gt=0).exists())
        self.assertConsistent(self.milk)

    def test_count_requests_are_unchanged(self):
        add_movement(self.milk, "5")
        self.authenticate(self.staff_a)

        response = self.client.post(
            ingredient_url("ingredient-count", self.milk),
            {"counted_quantity": "4", "expected_quantity": "5", "request_id": str(uuid.uuid4())},
        )

        self.assertEqual(response.status_code, 201)


class BatchListTests(ExpiryTestCase):
    def test_lists_batches_with_stock_earliest_expiry_first(self):
        add_movement(self.milk, "1")
        add_movement(self.milk, "1", expires_on=date(2026, 11, 20))
        add_movement(self.milk, "1", expires_on=date(2026, 11, 12))
        with at(NOW - timedelta(days=5)):
            add_movement(self.milk, "1", expires_on=date(2026, 11, 9))
        self.authenticate(self.staff_a)

        results = self.client.get(ingredient_url("ingredient-batches", self.milk)).json()["results"]

        self.assertEqual(
            [(item["expires_on"], item["status"]) for item in results],
            [("2026-11-09", "expired"), ("2026-11-12", "expiring"), ("2026-11-20", "ok"), (None, "no_expiry")],
        )

    def test_empty_batches_are_listed_only_on_request(self):
        add_movement(self.milk, "1", expires_on=date(2026, 11, 12))
        add_movement(self.milk, "-1", kind=MovementKind.COUNT)
        self.authenticate(self.staff_a)
        url = ingredient_url("ingredient-batches", self.milk)

        self.assertEqual(self.client.get(url).json()["count"], 0)
        self.assertEqual(self.client.get(url, {"include_empty": "true"}).json()["count"], 1)

    def test_other_tenants_get_404(self):
        add_movement(self.oil_b, "1")
        self.authenticate(self.owner_a)

        self.assertEqual(
            self.client.get(ingredient_url("ingredient-batches", self.oil_b, organization=self.org_a)).status_code,
            404,
        )
        self.assertEqual(self.client.get(ingredient_url("ingredient-batches", self.oil_b)).status_code, 404)


class IngredientExpiryFieldsTests(ExpiryTestCase):
    def test_reports_next_expiry_and_expired_stock(self):
        with at(NOW - timedelta(days=5)):
            add_movement(self.milk, "2", expires_on=date(2026, 11, 9))
        add_movement(self.milk, "3", expires_on=date(2026, 11, 12))
        add_movement(self.milk, "1", expires_on=date(2026, 11, 11))
        add_movement(self.milk, "-1", kind=MovementKind.COUNT)
        self.authenticate(self.staff_a)

        item = self.client.get(org_url("ingredient-list", self.org_a)).json()["results"][0]

        self.assertEqual(item["next_expiry"], "2026-11-09")
        self.assertEqual(item["expiry_status"], "expired")
        self.assertEqual(item["expired_quantity"], "1.000")

    def test_empty_batches_and_undated_stock_are_ignored(self):
        add_movement(self.milk, "1", expires_on=date(2026, 11, 12))
        add_movement(self.milk, "-1", kind=MovementKind.COUNT)
        add_movement(self.milk, "5")
        self.authenticate(self.staff_a)

        item = self.client.get(ingredient_url("ingredient-detail", self.milk)).json()

        self.assertIsNone(item["next_expiry"])
        self.assertIsNone(item["expiry_status"])
        self.assertEqual(item["expired_quantity"], "0.000")

    def test_expired_means_before_the_organizations_local_date(self):
        add_movement(self.milk, "2", expires_on=TODAY_UTC)
        self.authenticate(self.staff_a)
        url = ingredient_url("ingredient-detail", self.milk)

        self.assertEqual(self.client.get(url).json()["expired_quantity"], "0.000")
        self.org_a.time_zone = "Asia/Kolkata"
        self.org_a.save()
        self.assertEqual(self.client.get(url).json()["expired_quantity"], "2.000")

    def test_edit_response_includes_expiry_fields(self):
        add_movement(self.milk, "2", expires_on=date(2026, 11, 12))
        self.authenticate(self.owner_a)

        item = self.client.patch(ingredient_url("ingredient-detail", self.milk), {"reorder_level": "1"}).json()

        self.assertEqual(item["next_expiry"], "2026-11-12")


class ExpiringListTests(ExpiryTestCase):
    url = property(lambda self: org_url("expiring-batches", self.org_a))

    def test_lists_expired_and_soon_expiring_stock(self):
        flour = Ingredient.objects.create(organization=self.org_a, name="Flour", unit=Unit.KILOGRAM)
        with at(NOW - timedelta(days=5)):
            add_movement(self.milk, "2", expires_on=date(2026, 11, 9))
        add_movement(flour, "5", expires_on=date(2026, 11, 13))
        add_movement(flour, "5", expires_on=date(2026, 11, 14))
        add_movement(self.milk, "1")
        add_movement(self.oil_b, "1", expires_on=date(2026, 11, 11))
        self.authenticate(self.staff_a)

        results = self.client.get(self.url).json()["results"]

        self.assertEqual(
            [(item["ingredient"]["name"], item["expires_on"], item["status"]) for item in results],
            [("Milk", "2026-11-09", "expired"), ("Flour", "2026-11-13", "expiring")],
        )

    def test_window_is_adjustable(self):
        add_movement(self.milk, "1", expires_on=date(2026, 11, 10))
        add_movement(self.milk, "1", expires_on=date(2026, 11, 25))
        self.authenticate(self.staff_a)

        self.assertEqual(self.client.get(self.url, {"within": "0"}).json()["count"], 1)
        self.assertEqual(self.client.get(self.url, {"within": "30"}).json()["count"], 2)

    def test_rejects_invalid_windows(self):
        self.authenticate(self.staff_a)
        for value in ("-1", "31", "soon", "1.5"):
            with self.subTest(within=value):
                self.assertEqual(self.client.get(self.url, {"within": value}).status_code, 400)

    def test_empty_batches_are_left_out(self):
        add_movement(self.milk, "1", expires_on=date(2026, 11, 11))
        add_movement(self.milk, "-1", kind=MovementKind.COUNT)
        self.authenticate(self.staff_a)

        self.assertEqual(self.client.get(self.url).json()["count"], 0)

    def test_other_tenants_get_404(self):
        self.authenticate(self.owner_b)

        self.assertEqual(self.client.get(self.url).status_code, 404)


class ExpiryCorrectionTests(ExpiryTestCase):
    def setUp(self):
        super().setUp()
        self.batch = add_movement(self.milk, "4", expires_on=date(2026, 11, 14)).created_batches.get()
        self.url = org_url("batch-detail", self.org_a, ingredient_pk=self.milk.pk, pk=self.batch.pk)

    def test_managers_correct_expiry_dates_on_the_record(self):
        self.authenticate(self.manager_a)

        response = self.client.patch(self.url, {"expires_on": "2026-11-16", "note": "Misread label"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["expires_on"], "2026-11-16")
        correction = self.milk.movements.get(kind=MovementKind.EXPIRY_CORRECTION)
        self.assertEqual(correction.quantity_change, 0)
        self.assertEqual(correction.corrected_batch, self.batch)
        self.assertEqual(correction.expires_on, date(2026, 11, 16))
        self.assertEqual(correction.created_by, self.manager_a)
        self.assertEqual(
            correction.note, "Expiry date changed from 2026-11-14 to 2026-11-16. Misread label"
        )
        self.assertFalse(correction.lines.exists())
        self.assertConsistent(self.milk)

    def test_only_managers_and_owners_correct(self):
        for user, expected in [(self.staff_a, 403), (self.owner_b, 404), (self.owner_a, 200)]:
            with self.subTest(user=user.email):
                self.authenticate(user)
                self.assertEqual(self.client.patch(self.url, {"expires_on": "2026-11-15"}).status_code, expected)

    def test_setting_the_same_date_records_nothing(self):
        self.authenticate(self.manager_a)

        self.client.patch(self.url, {"expires_on": "2026-11-14"})

        self.assertFalse(self.milk.movements.filter(kind=MovementKind.EXPIRY_CORRECTION).exists())

    def test_date_can_be_cleared_or_in_the_past_but_not_too_far_ahead(self):
        self.authenticate(self.manager_a)

        self.assertEqual(self.client.patch(self.url, {"expires_on": None}, format="json").status_code, 200)
        self.assertEqual(self.client.patch(self.url, {"expires_on": "2026-11-01"}).status_code, 200)
        self.assertEqual(self.client.patch(self.url, {"expires_on": "2040-01-01"}).status_code, 400)
        self.assertEqual(self.client.patch(self.url, {}).status_code, 400)
        self.batch.refresh_from_db()
        self.assertEqual(self.batch.expires_on, date(2026, 11, 1))

    def test_cannot_reach_another_tenants_or_another_ingredients_batch(self):
        oil_batch = add_movement(self.oil_b, "1", expires_on=date(2026, 11, 14)).created_batches.get()
        flour = Ingredient.objects.create(organization=self.org_a, name="Flour", unit=Unit.KILOGRAM)
        self.authenticate(self.owner_a)

        through_own = org_url("batch-detail", self.org_a, ingredient_pk=self.oil_b.pk, pk=oil_batch.pk)
        mixed = org_url("batch-detail", self.org_a, ingredient_pk=self.milk.pk, pk=oil_batch.pk)
        wrong_ingredient = org_url("batch-detail", self.org_a, ingredient_pk=flour.pk, pk=self.batch.pk)

        for url in (through_own, mixed, wrong_ingredient):
            with self.subTest(url=url):
                self.assertEqual(self.client.patch(url, {"expires_on": "2026-11-20"}).status_code, 404)
        oil_batch.refresh_from_db()
        self.assertEqual(oil_batch.expires_on, date(2026, 11, 14))

    def test_corrections_appear_in_the_history(self):
        self.authenticate(self.manager_a)
        with at(NOW + timedelta(minutes=1)):
            self.client.patch(self.url, {"expires_on": "2026-11-16"})

        latest =self.client.get(ingredient_url("ingredient-movements", self.milk)).json()["results"][0]

        self.assertEqual(latest["kind"], "expiry_correction")
        self.assertEqual(latest["corrected_batch"], str(self.batch.pk))


class BatchAdminTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin_user = User.objects.create_superuser(email="admin@example.com", password=PASSWORD)
        organization = Organization.objects.create(name="Bakery")
        milk = Ingredient.objects.create(organization=organization, name="Milk", unit=Unit.LITRE)
        cls.movement = add_movement(milk, "2", expires_on=date(2030, 1, 1))
        cls.batch = cls.movement.created_batches.get()

    def setUp(self):
        self.client.force_login(self.admin_user)

    def test_batches_are_view_only(self):
        change_url = reverse("admin:inventory_stockbatch_change", args=[self.batch.pk])

        self.assertEqual(self.client.get(change_url, secure=True).status_code, 200)
        self.assertEqual(
            self.client.post(change_url, {"quantity_remaining": "99"}, secure=True).status_code, 403
        )
        self.assertEqual(
            self.client.post(
                reverse("admin:inventory_stockbatch_delete", args=[self.batch.pk]), {"post": "yes"}, secure=True
            ).status_code,
            403,
        )
        self.assertEqual(self.client.get(reverse("admin:inventory_stockbatch_add"), secure=True).status_code, 403)
        self.batch.refresh_from_db()
        self.assertEqual(self.batch.quantity_remaining, Decimal("2"))

    def test_movement_lines_are_view_only(self):
        request = RequestFactory().get("/")
        request.user = self.admin_user
        movement_admin = admin.site._registry[StockMovement]

        inline = movement_admin.get_inline_instances(request, self.movement)[0]

        self.assertIs(inline.model, MovementLine)
        self.assertFalse(inline.has_add_permission(request, self.movement))
        self.assertFalse(inline.has_change_permission(request, self.movement))
        self.assertFalse(inline.has_delete_permission(request, self.movement))
        self.assertEqual(
            self.client.get(
                reverse("admin:inventory_stockmovement_change", args=[self.movement.pk]), secure=True
            ).status_code,
            200,
        )

    def test_lines_cannot_be_changed(self):
        line = self.movement.lines.get()
        line.quantity_change = Decimal("5")

        with self.assertRaisesMessage(ValueError, "Movement lines cannot be changed."):
            line.save()


class OpeningBatchMigrationTests(TransactionTestCase):
    """
    Stock recorded before batch tracking moves into one opening batch per
    ingredient. The Phase 4a movements themselves are left unchanged.
    """

    before = [("inventory", "0001_initial"), ("organizations", "0001_initial")]

    def migrate(self, targets):
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(targets)
        return executor.loader.project_state(targets).apps

    def latest(self):
        return MigrationExecutor(connection).loader.graph.leaf_nodes()

    def test_existing_stock_moves_into_opening_batches(self):
        old = self.migrate(self.before)
        self.addCleanup(self.migrate, self.latest())
        Organization0001 = old.get_model("organizations", "Organization")
        Ingredient0001 = old.get_model("inventory", "Ingredient")
        Movement0001 = old.get_model("inventory", "StockMovement")
        organization = Organization0001.objects.create(name="Bakery")
        flour = Ingredient0001.objects.create(
            organization=organization, name="Flour", unit="kg", quantity_on_hand=Decimal("5")
        )
        Ingredient0001.objects.create(organization=organization, name="Salt", unit="kg")
        Movement0001.objects.create(
            ingredient=flour, kind="receive", quantity_change=Decimal("5"),
            quantity_after=Decimal("5"), request_id=uuid.uuid4(),
        )

        self.migrate(self.latest())

        organization = Organization.objects.get()
        self.assertEqual(organization.time_zone, "UTC")
        flour = Ingredient.objects.get(name="Flour")
        opening = flour.batches.get()
        self.assertEqual(opening.source, BatchSource.OPENING)
        self.assertEqual(opening.quantity_received, Decimal("5"))
        self.assertEqual(opening.quantity_remaining, Decimal("5"))
        self.assertIsNone(opening.expires_on)
        self.assertIsNone(opening.source_movement)
        self.assertFalse(Ingredient.objects.get(name="Salt").batches.exists())
        legacy = flour.movements.get()
        self.assertIs(legacy.tracks_batches, False)
        self.assertFalse(legacy.lines.exists())
        self.assertEqual(stock_problems(flour), [])

        # New movements draw on the opening batch and keep everything consistent.
        add_movement(flour, "-2", kind=MovementKind.COUNT)
        add_movement(flour, "1", expires_on=date(2030, 1, 1))
        flour.refresh_from_db()
        self.assertEqual(stock_problems(flour), [])
        opening.refresh_from_db()
        self.assertEqual(opening.quantity_remaining, Decimal("3"))

    def test_opening_batches_are_removed_when_migrating_back(self):
        old = self.migrate(self.before)
        self.addCleanup(self.migrate, self.latest())
        organization = old.get_model("organizations", "Organization").objects.create(name="Bakery")
        old.get_model("inventory", "Ingredient").objects.create(
            organization=organization, name="Flour", unit="kg", quantity_on_hand=Decimal("5")
        )
        self.migrate(self.latest())
        self.assertEqual(StockBatch.objects.count(), 1)

        middle = self.migrate([("inventory", "0002_stock_batches")])

        self.assertEqual(middle.get_model("inventory", "StockBatch").objects.count(), 0)
