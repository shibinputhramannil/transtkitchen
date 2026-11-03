from django.urls import path

from . import views

waste = "organizations/<uuid:organization_id>/waste/"

urlpatterns = [
    path(waste, views.WasteListCreateView.as_view(), name="waste-list"),
    path(f"{waste}summary/", views.WasteSummaryView.as_view(), name="waste-summary"),
    path(f"{waste}<uuid:pk>/", views.WasteDetailView.as_view(), name="waste-detail"),
    path(f"{waste}<uuid:pk>/void/", views.VoidWasteView.as_view(), name="waste-void"),
]
