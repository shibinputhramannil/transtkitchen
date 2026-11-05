from rest_framework import generics
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.views import (
    TokenBlacklistView,
    TokenObtainPairView,
    TokenRefreshView,
)

from .serializers import PasswordChangeSerializer, RegisterSerializer, UserSerializer


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


class PasswordChangeView(generics.GenericAPIView):
    """
    Changes the signed-in user's password: {current_password, new_password}.
    Every token issued before the change stops working (other devices are
    signed out); the response holds a fresh access and refresh token for this one.
    """

    serializer_class = PasswordChangeSerializer
    throttle_scope = "auth"

    def post(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = request.user
        user.set_password(serializer.validated_data["new_password"])
        user.save(update_fields=["password"])
        refresh = RefreshToken.for_user(user)
        return Response({"access": str(refresh.access_token), "refresh": str(refresh)})


class MeView(generics.RetrieveUpdateAPIView):
    serializer_class = UserSerializer
    http_method_names = ["get", "patch", "head", "options"]

    def get_object(self):
        return self.request.user
