from django.db import IntegrityError, transaction
from django.shortcuts import get_object_or_404
from rest_framework import generics
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import SAFE_METHODS
from rest_framework.response import Response

from apps.organizations.mixins import OrganizationScopedMixin
from apps.organizations.models import Role

from .models import MenuItem, RecipeLine
from .serializers import DUPLICATE_NAME, MenuItemSerializer, RecipeSerializer


class MenuScopedMixin(OrganizationScopedMixin):
    """Menu items of the requester's organization; staff read, managers edit."""

    def get_minimum_role(self):
        return Role.STAFF if self.request.method in SAFE_METHODS else Role.MANAGER

    def get_serializer_context(self):
        return {**super().get_serializer_context(), "organization": self.organization}

    def menu_items(self):
        return MenuItem.objects.filter(organization=self.organization).prefetch_related(
            "recipe_lines__ingredient"
        )

    def save_menu_item(self, serializer, **kwargs):
        try:
            with transaction.atomic():
                serializer.save(**kwargs)
        except IntegrityError:
            raise ValidationError({"name": [DUPLICATE_NAME]}) from None


class MenuItemListCreateView(MenuScopedMixin, generics.ListCreateAPIView):
    """
    Lists active menu items by name. ?search= matches part of the name;
    ?archived=true lists archived items instead.
    """

    serializer_class = MenuItemSerializer

    def get_queryset(self):
        archived = self.request.query_params.get("archived") == "true"
        queryset = self.menu_items().filter(is_active=not archived)
        search = self.request.query_params.get("search", "").strip()
        if search:
            queryset = queryset.filter(name__icontains=search)
        return queryset

    def perform_create(self, serializer):
        self.save_menu_item(serializer, organization=self.organization)


class MenuItemDetailView(MenuScopedMixin, generics.RetrieveUpdateDestroyAPIView):
    """A menu item can be deleted only while no imported sales refer to it; archive it otherwise."""

    serializer_class = MenuItemSerializer
    http_method_names = ["get", "patch", "delete", "head", "options"]

    def get_queryset(self):
        return self.menu_items()

    def perform_update(self, serializer):
        self.save_menu_item(serializer)

    def perform_destroy(self, instance):
        if instance.sale_lines.exists() or instance.waste_entries.exists() or instance.prep_plan_lines.exists():
            raise ValidationError(
                {"detail": "Sales, waste, or prep plans refer to this menu item, so it cannot be deleted. Archive it instead."}
            )
        instance.delete()


class RecipeView(MenuScopedMixin, generics.GenericAPIView):
    """
    GET returns the menu item with its recipe. PUT replaces the whole recipe
    at once: {"lines": [{"ingredient", "quantity", "unit"}, ...]}.
    """

    serializer_class = RecipeSerializer
    http_method_names = ["get", "put", "head", "options"]

    def get(self, request, *args, **kwargs):
        menu_item = get_object_or_404(self.menu_items(), pk=kwargs["pk"])
        return Response(MenuItemSerializer(menu_item, context=self.get_serializer_context()).data)

    def put(self, request, *args, **kwargs):
        # Look the item up first, so another organization's item is a 404 whatever the body says.
        get_object_or_404(MenuItem.objects.filter(organization=self.organization), pk=kwargs["pk"])
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        with transaction.atomic():
            menu_item = get_object_or_404(
                MenuItem.objects.filter(organization=self.organization).select_for_update(),
                pk=kwargs["pk"],
            )
            menu_item.recipe_lines.all().delete()
            RecipeLine.objects.bulk_create(
                RecipeLine(menu_item=menu_item, **line) for line in serializer.validated_data["lines"]
            )
        menu_item = self.menu_items().get(pk=menu_item.pk)
        return Response(MenuItemSerializer(menu_item, context=self.get_serializer_context()).data)
