from rest_framework import generics
from rest_framework.permissions import AllowAny
from rest_framework_simplejwt.views import (
    TokenBlacklistView,
    TokenObtainPairView,
    TokenRefreshView,
)

from .serializers import RegisterSerializer, UserSerializer


class RegisterView(generics.CreateAPIView):
    """Creates an account. Organizations are created separately once signed in."""

    authentication_classes = []
    permission_classes = [AllowAny]
    serializer_class = RegisterSerializer
    throttle_scope = "auth"


class LoginView(TokenObtainPairView):
    """Exchanges an email and password for an access and a refresh token."""

    throttle_scope = "auth"


class RefreshView(TokenRefreshView):
    """Returns a new access token and a new refresh token; the old one stops working."""


class LogoutView(TokenBlacklistView):
    """Revokes the given refresh token."""


class MeView(generics.RetrieveUpdateAPIView):
    serializer_class = UserSerializer
    http_method_names = ["get", "patch", "head", "options"]

    def get_object(self):
        return self.request.user
