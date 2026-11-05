import json
import logging
from unittest import mock

from django.db import OperationalError, connection
from django.test import TestCase, override_settings
from django.test.utils import isolate_apps
from django.urls import reverse
from rest_framework.test import APITestCase

from . import csp_reports
from .models import TimeStampedModel
from .testing import APITestCase as HttpsAPITestCase


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


FRONTEND = "https://web.example.com"
CHROME = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/142.0.0.0 Safari/537.36"
)
ORG_ID = "6555e453-7b07-48f8-8ad4-f7a202bce6b5"


def report_uri_body(**overrides):
    report = {
        "document-uri": f"{FRONTEND}/organizations/{ORG_ID}/inventory?tab=history#batches",
        "blocked-uri": "inline",
        "effective-directive": "script-src-elem",
        "violated-directive": "script-src-elem",
        "disposition": "report",
        "status-code": 200,
        "source-file": f"{FRONTEND}/assets/index-abc.js?v=1",
        "line-number": 3,
        "column-number": 14,
        "referrer": f"{FRONTEND}/account?token=secret-token",
        "script-sample": "alert('owner@example.com')",
        "original-policy": "default-src 'self'; script-src 'self'",
    }
    report.update(overrides)
    return {"csp-report": report}


def reporting_api_report(**body_overrides):
    body = {
        "documentURL": f"{FRONTEND}/organizations/{ORG_ID}/sales",
        "blockedURL": "https://cdn.example.net/library.js?key=abc",
        "effectiveDirective": "script-src-elem",
        "disposition": "enforce",
        "statusCode": 200,
        "sourceFile": f"{FRONTEND}/assets/index-abc.js",
        "lineNumber": 9,
        "columnNumber": 2,
        "sample": "",
        "referrer": "",
    }
    body.update(body_overrides)
    return {"type": "csp-violation", "age": 10, "url": body["documentURL"], "user_agent": CHROME, "body": body}


@override_settings(CORS_ALLOWED_ORIGINS=[FRONTEND])
class CspReportTests(HttpsAPITestCase):
    url = reverse("csp-reports")
    logger_name = "apps.core.csp_reports"

    def setUp(self):
        super().setUp()  # HTTPS requests, empty rate-limit counters
        csp_reports.reset_state()

    def send(self, payload, content_type="application/csp-report", **extra):
        body = payload if isinstance(payload, (bytes, str)) else json.dumps(payload)
        extra.setdefault("HTTP_USER_AGENT", CHROME)
        return self.client.post(self.url, data=body, content_type=content_type, **extra)

    def messages(self, logs):
        return [record.getMessage() for record in logs.records]

    def test_logs_only_a_minimal_summary_of_a_report(self):
        with self.assertLogs(self.logger_name, level="INFO") as logs:
            response = self.send(report_uri_body(), REMOTE_ADDR="203.0.113.9")

        self.assertEqual(response.status_code, 204)
        self.assertEqual(
            self.messages(logs),
            [
                "csp-violation disposition=report directive=script-src-elem blocked=inline "
                "page=/organizations/:id/inventory source=https://web.example.com/assets/index-abc.js:3:14 "
                'status=200 browser="Chrome 142"'
            ],
        )
        logged = " ".join(logs.output)
        private_values = (
            "203.0.113.9", ORG_ID, "tab=history", "batches", "secret-token", "owner@example.com",
            "alert", "Mozilla", "default-src", "v=1", "account",
        )
        for private in private_values:
            with self.subTest(private=private):
                self.assertNotIn(private, logged)

    def test_accepts_reporting_api_reports_alone_or_in_a_list(self):
        with self.assertLogs(self.logger_name, level="INFO") as logs:
            single = self.send(reporting_api_report(), content_type="application/reports+json")
            listed = self.send(
                [
                    reporting_api_report(effectiveDirective="img-src", blockedURL="data:image/png;base64,AAAA"),
                    reporting_api_report(effectiveDirective="connect-src", blockedURL="blob:https://web.example.com/x"),
                ],
                content_type="application/reports+json",
            )

        self.assertEqual((single.status_code, listed.status_code), (204, 204))
        tail = 'page=/organizations/:id/sales source=https://web.example.com/assets/index-abc.js:9:2 status=200 browser="Chrome 142"'
        self.assertEqual(
            self.messages(logs),
            [
                f"csp-violation disposition=enforce directive=script-src-elem blocked=https://cdn.example.net {tail}",
                f"csp-violation disposition=enforce directive=img-src blocked=data {tail}",
                f"csp-violation disposition=enforce directive=connect-src blocked=blob {tail}",
            ],
        )

    def test_reduces_blocked_resources_and_falls_back_to_the_violated_directive(self):
        cases = [
            ({"blocked-uri": "eval"}, "blocked=eval"),
            ({"blocked-uri": ""}, "blocked=none"),
            ({"blocked-uri": "https://cdn.example.net:8443/a/b.css?x=1"}, "blocked=https://cdn.example.net:8443"),
            ({"blocked-uri": "wss://socket.example.net/feed"}, "blocked=wss://socket.example.net"),
            ({"blocked-uri": "javascript:alert(1)"}, "blocked=other"),
            ({"effective-directive": None, "violated-directive": "img-src 'self'"}, "directive=img-src"),
        ]
        for overrides, expected in cases:
            with self.subTest(overrides=overrides):
                csp_reports.reset_state()
                with self.assertLogs(self.logger_name, level="INFO") as logs:
                    self.send(report_uri_body(**overrides))
                self.assertIn(expected, self.messages(logs)[0])

    def test_ignores_other_sites_and_browser_extensions(self):
        ignored = [
            report_uri_body(**{"document-uri": "https://other.example.org/page"}),
            report_uri_body(**{"blocked-uri": "chrome-extension://abcdef/content.js"}),
            report_uri_body(**{"source-file": "moz-extension://abc/inject.js"}),
            report_uri_body(**{"blocked-uri": "safari-web-extension://abc/x.js"}),
        ]
        with self.assertNoLogs(self.logger_name, level="INFO"):
            for payload in ignored:
                self.assertEqual(self.send(payload).status_code, 204)

    def test_source_files_from_other_origins_keep_only_their_origin(self):
        with self.assertLogs(self.logger_name, level="INFO") as logs:
            self.send(report_uri_body(**{"source-file": "https://cdn.example.net/x/y.js?token=1", "line-number": 5}))

        self.assertIn("source=https://cdn.example.net status=", self.messages(logs)[0])

    def test_neutralizes_invalid_or_injected_values(self):
        payload = report_uri_body(**{
            "effective-directive": "script-src\nFAKE=1",
            "disposition": "maybe",
            "status-code": "abc",
            "line-number": -5,
            "document-uri": f"{FRONTEND}/a\nb c",
        })
        with self.assertLogs(self.logger_name, level="INFO") as logs:
            self.send(payload)

        message = self.messages(logs)[0]
        # urlsplit already drops newlines from URLs; the space becomes "_".
        self.assertEqual(
            message,
            "csp-violation disposition=unknown directive=unknown blocked=inline page=/ab_c "
            'source=https://web.example.com/assets/index-abc.js status=- browser="Chrome 142"',
        )
        self.assertNotIn("\n", message)

    def test_logs_each_distinct_violation_once_per_hour(self):
        with mock.patch("apps.core.csp_reports._now") as now:
            with self.assertLogs(self.logger_name, level="INFO") as logs:
                now.return_value = 1000.0
                self.send(report_uri_body())
                now.return_value = 1500.0
                self.send(report_uri_body())
                now.return_value = 1000.0 + csp_reports.WINDOW_SECONDS
                self.send(report_uri_body())

        self.assertEqual(len(logs.records), 2)

    def test_limits_distinct_violations_per_hour(self):
        directives = ["script-src", "style-src", "img-src", "font-src"]
        with mock.patch.object(csp_reports, "MAX_KEYS_PER_WINDOW", 2):
            with self.assertLogs(self.logger_name, level="INFO") as logs:
                for directive in directives:
                    self.send(report_uri_body(**{"effective-directive": directive}))

        self.assertEqual([record.levelname for record in logs.records], ["INFO", "INFO", "WARNING"])
        self.assertIn("limit reached", logs.records[-1].getMessage())

    def test_rejects_oversized_bodies(self):
        payload = report_uri_body(**{"script-sample": "x" * (csp_reports.MAX_BODY_BYTES + 1)})
        with self.assertNoLogs(self.logger_name, level="INFO"):
            response = self.send(payload)

        self.assertEqual(response.status_code, 413)

    def test_accepts_but_does_not_log_malformed_bodies(self):
        bodies = [
            b"not json",
            b"[1, 2]",
            json.dumps({"csp-report": "x"}),
            json.dumps({"type": "csp-violation", "body": "x"}),
            json.dumps({"type": "other", "body": {}}),
        ]
        with self.assertNoLogs(self.logger_name, level="INFO"):
            for body in bodies:
                with self.subTest(body=body):
                    self.assertEqual(self.send(body).status_code, 204)

    def test_needs_no_sign_in_and_ignores_authorization_headers(self):
        response = self.send(report_uri_body(), HTTP_AUTHORIZATION="Bearer not-a-token")

        self.assertEqual(response.status_code, 204)

    def test_only_accepts_post(self):
        self.assertEqual(self.client.get(self.url).status_code, 405)

    def test_is_rate_limited(self):
        statuses = [self.send(report_uri_body()).status_code for _ in range(61)]

        self.assertEqual(statuses[:60], [204] * 60)
        self.assertEqual(statuses[60], 429)

    def test_allows_cross_origin_reports_from_the_frontend_only(self):
        preflight = {"HTTP_ACCESS_CONTROL_REQUEST_METHOD": "POST", "HTTP_ACCESS_CONTROL_REQUEST_HEADERS": "content-type"}
        allowed = self.client.options(self.url, HTTP_ORIGIN=FRONTEND, **preflight)
        other = self.client.options(self.url, HTTP_ORIGIN="https://evil.example.com", **preflight)

        self.assertEqual(allowed["Access-Control-Allow-Origin"], FRONTEND)
        self.assertIn("content-type", allowed["Access-Control-Allow-Headers"])
        self.assertNotIn("Access-Control-Allow-Origin", other)

    def test_names_browser_families_and_major_versions_only(self):
        mac_safari = (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) "
            "Version/26.0 Safari/605.1.15"
        )
        iphone = "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) "
        cases = {
            CHROME: "Chrome 142",
            CHROME + " Edg/142.0.0.0": "Edge 142",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:144.0) Gecko/20100101 Firefox/144.0": "Firefox 144",
            mac_safari: "Safari 26",
            iphone + "Version/18.0 Mobile/15E148 Safari/604.1": "Safari 18",
            iphone + "CriOS/142.0.0.0 Mobile/15E148 Safari/604.1": "Chrome 142",
            "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 (KHTML, like Gecko) SamsungBrowser/28.0 "
            "Chrome/130.0.0.0 Mobile Safari/537.36": "Samsung Internet 28",
            "curl/8.0": "other",
            "": "other",
        }
        for user_agent, expected in cases.items():
            with self.subTest(user_agent=user_agent):
                self.assertEqual(csp_reports.browser_from_user_agent(user_agent), expected)
