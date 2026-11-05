from django.urls import path

from .views import LoginView, LogoutView, MeView, PasswordChangeView, RefreshView, RegisterView

urlpatterns = [
    path("register/", RegisterView.as_view(), name="register"),
    path("token/", LoginView.as_view(), name="token"),
    path("token/refresh/", RefreshView.as_view(), name="token-refresh"),
    path("logout/", LogoutView.as_view(), name="logout"),
    path("me/", MeView.as_view(), name="me"),
    path("password/", PasswordChangeView.as_view(), name="password-change"),
]
