from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, TestCase

from apps.inventory.models import Ingredient, Unit
from apps.inventory.tests import add_movement, ingredient_url
from apps.inventory.units import compatible, convert, round_quantity
from apps.organizations.models import Organization
from apps.organizations.testing import TenantTestCase, org_url

from .models import MenuItem, RecipeLine


class UnitTests(SimpleTestCase):
    def test_converts_within_a_kind_of_measure_only(self):
        self.assertEqual(convert(Decimal("120"), "g", "kg"), Decimal("0.12"))
        self.assertEqual(convert(Decimal("1.5"), "l", "ml"), Decimal("1500"))
        self.assertTrue(compatible("each", "each"))
        for pair in [("g", "ml"), ("kg", "each"), ("l", "g")]:
            with self.subTest(pair=pair):
                self.assertFalse(compatible(*pair))
                with self.assertRaises(ValueError):
                    convert(Decimal("1"), *pair)

    def test_rounds_half_up_to_three_places(self):
        self.assertEqual(round_quantity(Decimal("0.0005")), Decimal("0.001"))
        self.assertEqual(round_quantity(Decimal("0.0004999")), Decimal("0.000"))


class MenuTestCase(TenantTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.flour = Ingredient.objects.create(organization=cls.org_a, name="Flour", unit=Unit.KILOGRAM)
        cls.milk = Ingredient.objects.create(organization=cls.org_a, name="Milk", unit=Unit.LITRE)
        cls.oil_b = Ingredient.objects.create(organization=cls.org_b, name="Oil", unit=Unit.LITRE)
        cls.pizza = MenuItem.objects.create(organization=cls.org_a, name="Margherita Pizza")
        cls.salad_b = MenuItem.objects.create(organization=cls.org_b, name="Salad")

    def recipe_url(self, item, organization=None):
        return org_url("menu-item-recipe", organization or item.organization, pk=item.pk)

    def put_recipe(self, item, lines, organization=None):
        return self.client.put(self.recipe_url(item, organization), {"lines": lines}, format="json")


class MenuItemApiTests(MenuTestCase):
    list_url = property(lambda self: org_url("menu-item-list", self.org_a))

    def test_members_see_only_their_menu(self):
        self.authenticate(self.staff_a)

        names = [item["name"] for item in self.client.get(self.list_url).json()["results"]]

        self.assertEqual(names, ["Margherita Pizza"])

    def test_only_managers_and_owners_create(self):
        for user, name, expected in [(self.staff_a, "Soup", 403), (self.manager_a, "Soup", 201), (self.owner_b, "Tea", 404)]:
            with self.subTest(user=user.email):
                self.authenticate(user)
                self.assertEqual(self.client.post(self.list_url, {"name": name}).status_code, expected)

    def test_names_are_unique_per_organization_ignoring_case(self):
        self.authenticate(self.owner_a)
        self.assertEqual(self.client.post(self.list_url, {"name": "margherita pizza"}).status_code, 400)

        self.authenticate(self.owner_b)
        response = self.client.post(org_url("menu-item-list", self.org_b), {"name": "Margherita Pizza"})
        self.assertEqual(response.status_code, 201)

    def test_cannot_reach_another_tenants_menu_item(self):
        self.authenticate(self.owner_a)
        url = org_url("menu-item-detail", self.org_a, pk=self.salad_b.pk)

        self.assertEqual(self.client.get(url).status_code, 404)
        self.assertEqual(self.client.patch(url, {"name": "Mine"}).status_code, 404)
        self.assertEqual(self.client.delete(url).status_code, 404)

    def test_archive_and_delete(self):
        soup = MenuItem.objects.create(organization=self.org_a, name="Soup")
        self.authenticate(self.manager_a)

        archived = self.client.patch(org_url("menu-item-detail", self.org_a, pk=self.pizza.pk), {"is_active": False})
        deleted = self.client.delete(org_url("menu-item-detail", self.org_a, pk=soup.pk))

        self.assertEqual(archived.json()["is_active"], False)
        self.assertEqual(deleted.status_code, 204)
        self.assertEqual(
            [i["name"] for i in self.client.get(self.list_url, {"archived": "true"}).json()["results"]],
            ["Margherita Pizza"],
        )


class RecipeApiTests(MenuTestCase):
    def test_owner_sets_recipe_with_compatible_units(self):
        self.authenticate(self.owner_a)

        response = self.put_recipe(
            self.pizza,
            [
                {"ingredient": str(self.flour.pk), "quantity": "250", "unit": "g"},
                {"ingredient": str(self.milk.pk), "quantity": "0.05", "unit": "l"},
            ],
        )

        self.assertEqual(response.status_code, 200)
        recipe = {line["ingredient"]["name"]: (line["quantity"], line["unit"]) for line in response.json()["recipe"]}
        self.assertEqual(recipe, {"Flour": ("250.000", "g"), "Milk": ("0.050", "l")})

    def test_put_replaces_the_whole_recipe(self):
        RecipeLine.objects.create(menu_item=self.pizza, ingredient=self.milk, quantity=1, unit="l")
        self.authenticate(self.owner_a)

        self.put_recipe(self.pizza, [{"ingredient": str(self.flour.pk), "quantity": "1", "unit": "kg"}])

        self.assertEqual(list(self.pizza.recipe_lines.values_list("ingredient__name", flat=True)), ["Flour"])

    def test_rejects_incompatible_units(self):
        self.authenticate(self.owner_a)

        response = self.put_recipe(self.pizza, [{"ingredient": str(self.flour.pk), "quantity": "1", "unit": "ml"}])

        self.assertEqual(response.status_code, 400)
        self.assertFalse(self.pizza.recipe_lines.exists())

    def test_rejects_another_tenants_ingredient(self):
        self.authenticate(self.owner_a)

        response = self.put_recipe(self.pizza, [{"ingredient": str(self.oil_b.pk), "quantity": "1", "unit": "l"}])

        self.assertEqual(response.status_code, 400)
        self.assertFalse(self.pizza.recipe_lines.exists())

    def test_rejects_duplicate_ingredients_and_bad_quantities(self):
        self.authenticate(self.owner_a)
        line = {"ingredient": str(self.flour.pk), "quantity": "1", "unit": "kg"}

        self.assertEqual(self.put_recipe(self.pizza, [line, line]).status_code, 400)
        for quantity in ("0", "-1", "1.0001", "abc"):
            with self.subTest(quantity=quantity):
                self.assertEqual(self.put_recipe(self.pizza, [{**line, "quantity": quantity}]).status_code, 400)

    def test_empty_recipe_is_allowed(self):
        RecipeLine.objects.create(menu_item=self.pizza, ingredient=self.milk, quantity=1, unit="l")
        self.authenticate(self.owner_a)

        self.assertEqual(self.put_recipe(self.pizza, []).status_code, 200)
        self.assertFalse(self.pizza.recipe_lines.exists())

    def test_roles_and_tenants(self):
        line = [{"ingredient": str(self.flour.pk), "quantity": "1", "unit": "kg"}]
        self.authenticate(self.staff_a)
        self.assertEqual(self.client.get(self.recipe_url(self.pizza)).status_code, 200)
        self.assertEqual(self.put_recipe(self.pizza, line).status_code, 403)

        self.authenticate(self.owner_b)
        self.assertEqual(self.put_recipe(self.pizza, line).status_code, 404)
        self.assertEqual(self.put_recipe(self.pizza, line, organization=self.org_b).status_code, 404)


class IngredientGuardTests(MenuTestCase):
    def setUp(self):
        super().setUp()
        RecipeLine.objects.create(menu_item=self.pizza, ingredient=self.flour, quantity=250, unit="g")
        self.authenticate(self.owner_a)

    def test_unit_can_change_only_within_its_kind_of_measure(self):
        url = ingredient_url("ingredient-detail", self.flour)

        self.assertEqual(self.client.patch(url, {"unit": "l"}).status_code, 400)
        self.assertEqual(self.client.patch(url, {"unit": "g"}).status_code, 200)

    def test_ingredient_used_in_a_recipe_cannot_be_deleted(self):
        response = self.client.delete(ingredient_url("ingredient-detail", self.flour))

        self.assertEqual(response.status_code, 400)
        self.assertTrue(Ingredient.objects.filter(pk=self.flour.pk).exists())


class RecipeLineCleanTests(TestCase):
    def test_admin_rules_match_the_api(self):
        bakery = Organization.objects.create(name="Bakery")
        other = Organization.objects.create(name="Other")
        item = MenuItem.objects.create(organization=bakery, name="Bread")
        flour = Ingredient.objects.create(organization=bakery, name="Flour", unit=Unit.KILOGRAM)
        foreign = Ingredient.objects.create(organization=other, name="Flour", unit=Unit.KILOGRAM)

        with self.assertRaises(ValidationError):
            RecipeLine(menu_item=item, ingredient=flour, quantity=1, unit="ml").full_clean()
        with self.assertRaises(ValidationError):
            RecipeLine(menu_item=item, ingredient=foreign, quantity=1, unit="kg").full_clean()
        RecipeLine(menu_item=item, ingredient=flour, quantity=1, unit="g").full_clean()


class HistoryIndependenceTests(MenuTestCase):
    def test_unit_lock_from_stock_history_still_applies(self):
        add_movement(self.milk, "2")
        self.authenticate(self.owner_a)

        self.assertEqual(self.client.patch(ingredient_url("ingredient-detail", self.milk), {"unit": "ml"}).status_code, 400)
