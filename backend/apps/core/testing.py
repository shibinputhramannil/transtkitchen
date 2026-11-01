"""Shared helpers for API tests."""

from django.core.cache import cache
from rest_framework import test
from rest_framework_simplejwt.tokens import RefreshToken


class HttpsAPIClient(test.APIClient):
    """
    Marks requests as HTTPS the way Render's proxy does. Production settings
    trust this header (SECURE_PROXY_SSL_HEADER), so they do not redirect.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.defaults.setdefault("HTTP_X_FORWARDED_PROTO", "https")


class APITestCase(test.APITestCase):
    """
    Behaves the same with DEBUG on or off: requests use HTTPS, so production
    settings never redirect them, and rate-limit counters start empty.
    """

    client_class = HttpsAPIClient

    def setUp(self):
        super().setUp()
        cache.clear()

    def authenticate(self, user):
        """Signs the client in as `user` with a real JWT access token."""
        token = RefreshToken.for_user(user).access_token
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
