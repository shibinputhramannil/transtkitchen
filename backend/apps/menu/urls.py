from django.urls import path

from . import views

menu_items = "organizations/<uuid:organization_id>/menu-items/"

urlpatterns = [
    path(menu_items, views.MenuItemListCreateView.as_view(), name="menu-item-list"),
    path(f"{menu_items}<uuid:pk>/", views.MenuItemDetailView.as_view(), name="menu-item-detail"),
    path(f"{menu_items}<uuid:pk>/recipe/", views.RecipeView.as_view(), name="menu-item-recipe"),
]
