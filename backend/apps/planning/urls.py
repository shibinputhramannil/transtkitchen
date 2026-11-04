from django.urls import path

from . import views

organization = "organizations/<uuid:organization_id>/"
plans = f"{organization}prep-plans/"

urlpatterns = [
    path(f"{organization}forecast/", views.ForecastView.as_view(), name="forecast"),
    path(plans, views.PlanListCreateView.as_view(), name="prep-plan-list"),
    path(f"{plans}<uuid:pk>/", views.PlanDetailView.as_view(), name="prep-plan-detail"),
    path(f"{plans}<uuid:pk>/refresh/", views.PlanActionView.as_view(action="refresh"), name="prep-plan-refresh"),
    path(f"{plans}<uuid:pk>/finalize/", views.PlanActionView.as_view(action="finalize"), name="prep-plan-finalize"),
    path(f"{plans}<uuid:pk>/reopen/", views.PlanActionView.as_view(action="reopen"), name="prep-plan-reopen"),
]
