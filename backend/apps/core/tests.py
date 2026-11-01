import logging
from unittest import mock

from django.db import OperationalError, connection
from django.test import TestCase, override_settings
from django.test.utils import isolate_apps
from django.urls import reverse
from rest_framework.test import APITestCase

from .models import TimeStampedModel


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

    def test_responds_with_json_by_default(self):
        for accept in (None, "application/json", "*/*"):
            with self.subTest(accept=accept):
                headers = {"HTTP_ACCEPT": accept} if accept else {}
                response = self.client.get(self.url, **headers)

                self.assertEqual(response["Content-Type"], "application/json")


# Production-like HTTPS settings: Render's health checks arrive over plain HTTP.
@override_settings(SECURE_SSL_REDIRECT=True, SECURE_REDIRECT_EXEMPT=[r"^api/health/$"])
class HealthOverHttpTests(APITestCase):
    def test_health_is_served_without_https_redirect(self):
        response = self.client.get(reverse("health"))

        self.assertEqual(response.status_code, 200)

    def test_other_paths_redirect_to_https(self):
        response = self.client.get("/admin/")

        self.assertEqual(response.status_code, 301)
        self.assertTrue(response["Location"].startswith("https://"))


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


class TimeStampedModelTests(TestCase):
    def test_is_abstract(self):
        self.assertTrue(TimeStampedModel._meta.abstract)

    @isolate_apps("apps.core")
    def test_sets_created_and_updated_timestamps(self):
        class Sample(TimeStampedModel):
            class Meta:
                app_label = "core"

        # PostgreSQL DDL is transactional, so the table is dropped with the test transaction.
        with connection.schema_editor() as editor:
            editor.create_model(Sample)

        sample = Sample.objects.create()
        created_at, updated_at = sample.created_at, sample.updated_at
        self.assertIsNotNone(created_at)
        self.assertGreaterEqual(updated_at, created_at)

        sample.save()
        sample.refresh_from_db()
        self.assertEqual(sample.created_at, created_at)
        self.assertGreater(sample.updated_at, updated_at)
