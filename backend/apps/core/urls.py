from django.urls import path

from .csp_reports import CspReportView
from .views import HealthView

urlpatterns = [
    path("health/", HealthView.as_view(), name="health"),
    path("csp-reports/", CspReportView.as_view(), name="csp-reports"),
]
