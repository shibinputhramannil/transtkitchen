from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/", include("apps.core.urls")),
    path("api/auth/", include("apps.accounts.urls")),
    path("api/", include("apps.organizations.urls")),
    path("api/", include("apps.inventory.urls")),
    path("api/", include("apps.menu.urls")),
    path("api/", include("apps.sales.urls")),
    path("api/", include("apps.waste.urls")),
    path("api/", include("apps.planning.urls")),
]
