from django.urls import path

from . import views

organization = "organizations/<uuid:organization_id>/"
imports = f"{organization}sales-imports/"

urlpatterns = [
    path(imports, views.SalesImportListCreateView.as_view(), name="sales-import-list"),
    path(f"{imports}template.csv", views.TemplateView.as_view(), name="sales-import-template"),
    path(f"{imports}<uuid:pk>/", views.SalesImportDetailView.as_view(), name="sales-import-detail"),
    path(f"{imports}<uuid:pk>/rows/", views.SalesImportRowsView.as_view(), name="sales-import-rows"),
    path(f"{imports}<uuid:pk>/mappings/", views.MappingView.as_view(), name="sales-import-mappings"),
    path(f"{imports}<uuid:pk>/rematch/", views.RematchView.as_view(), name="sales-import-rematch"),
    path(f"{imports}<uuid:pk>/apply/", views.ApplyView.as_view(), name="sales-import-apply"),
    path(f"{imports}<uuid:pk>/discard/", views.DiscardView.as_view(), name="sales-import-discard"),
    path(f"{organization}sales/daily/", views.DailySalesView.as_view(), name="sales-daily"),
]
