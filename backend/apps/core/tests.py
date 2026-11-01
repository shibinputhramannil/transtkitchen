import logging
from unittest import mock

from django.db import OperationalError
from django.test import override_settings
from django.urls import reverse
from rest_framework.test import APITestCase


class HealthViewTests(APITestCase):
    url = reverse("health")

    def test_reports_ok_when_database_is_reachable(self):
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok", "database": "ok"})

    def test_reports_503_without_details_when_database_is_down(self):
        with (
            mock.patch("apps.core.views.connection.cursor", side_effect=OperationalError("boom")),
            self.assertLogs("apps.core.views", level="ERROR"),
        ):
            response = self.client.get(self.url)

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"status": "error", "database": "unavailable"})

    def test_rejects_write_methods(self):
        response = self.client.post(self.url)

        self.assertEqual(response.status_code, 405)


@override_settings(CORS_ALLOWED_ORIGINS=["https://app.example.com"])
class CorsTests(APITestCase):
    url = reverse("health")

    def test_allows_configured_frontend_origin(self):
        response = self.client.get(self.url, HTTP_ORIGIN="https://app.example.com")

        self.assertEqual(response["Access-Control-Allow-Origin"], "https://app.example.com")

    def test_ignores_unknown_origin(self):
        response = self.client.get(self.url, HTTP_ORIGIN="https://evil.example.com")

        self.assertNotIn("Access-Control-Allow-Origin", response)


class SecurityLoggingTests(APITestCase):
    url = reverse("health")

    def test_disallowed_host_is_rejected_without_error_log(self):
        with self.assertNoLogs(level="ERROR"):
            response = self.client.get(self.url, HTTP_HOST="evil.example.com")

        self.assertEqual(response.status_code, 400)

    def test_other_security_errors_still_reach_the_console(self):
        # Asserting on the root logger proves the record propagates to the console handler.
        with self.assertLogs(level="ERROR") as logs:
            logging.getLogger("django.security.SuspiciousOperation").error("suspicious")

        self.assertEqual(logs.records[0].name, "django.security.SuspiciousOperation")
