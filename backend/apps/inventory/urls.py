from django.urls import path

from . import views

organization = "organizations/<uuid:organization_id>/"
ingredients = f"{organization}ingredients/"

urlpatterns = [
    path(ingredients, views.IngredientListCreateView.as_view(), name="ingredient-list"),
    path(f"{ingredients}<uuid:pk>/", views.IngredientDetailView.as_view(), name="ingredient-detail"),
    path(f"{ingredients}<uuid:pk>/receive/", views.ReceiveView.as_view(), name="ingredient-receive"),
    path(f"{ingredients}<uuid:pk>/count/", views.CountView.as_view(), name="ingredient-count"),
    path(
        f"{ingredients}<uuid:pk>/movements/",
        views.StockMovementListView.as_view(),
        name="ingredient-movements",
    ),
    path(f"{ingredients}<uuid:pk>/batches/", views.StockBatchListView.as_view(), name="ingredient-batches"),
    path(
        f"{ingredients}<uuid:ingredient_pk>/batches/<uuid:pk>/",
        views.StockBatchDetailView.as_view(),
        name="batch-detail",
    ),
    path(f"{organization}expiring/", views.ExpiringBatchListView.as_view(), name="expiring-batches"),
]
