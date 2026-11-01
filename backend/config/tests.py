import json
import os
import subprocess
import sys
from io import StringIO
from unittest import mock

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command
from django.db import connection
from django.test import SimpleTestCase, TestCase
from rest_framework.permissions import IsAuthenticated
from rest_framework.settings import api_settings
from rest_framework.throttling import ScopedRateThrottle
from rest_framework_simplejwt.authentication import JWTAuthentication

from .env import env_bool, env_int, env_list, env_str

# Minimal valid configuration. The database is never contacted by these tests.
VALID_ENV = {
    "DJANGO_SECRET_KEY": "test-secret",
    "DATABASE_URL": "postgres://user:pass@localhost:5432/kitchenmind",
}
# Variables the interpreter itself needs; everything else is dropped.
PASSTHROUGH_ENV = ("PATH", "SYSTEMROOT", "HOME", "LANG", "TMPDIR", "TEMP", "TMP")


def load_settings(env, *names):
    """
    Load the settings in a fresh interpreter with exactly `env`, returning the
    values Django would use (including its defaults for anything not set).
    """
    code = (
        "import json, sys\n"
        "from django.conf import settings as s\n"
        "s.DEBUG  # settings load lazily; force it so configuration errors surface\n"
        "print(json.dumps({n: getattr(s, n) for n in sys.argv[1:]}, default=str))"
    )
    base = {key: os.environ[key] for key in PASSTHROUGH_ENV if key in os.environ}
    return subprocess.run(
        [sys.executable, "-c", code, *names],
        cwd=settings.BASE_DIR,
        env={
            **base,
            **env,
            "DJANGO_SETTINGS_MODULE": "config.settings",
            "PYTHONDONTWRITEBYTECODE": "1",
        },
        capture_output=True,
        text=True,
        timeout=60,
    )


class EnvHelperTests(SimpleTestCase):
    def test_env_bool_accepts_common_spellings(self):
        for raw, expected in [("true", True), (" Yes ", True), ("1", True), ("ON", True),
                              ("false", False), ("no", False), ("0", False), ("Off", False)]:
            with self.subTest(raw=raw), mock.patch.dict(os.environ, {"FLAG": raw}):
                self.assertIs(env_bool("FLAG"), expected)

    def test_env_bool_uses_default_when_unset_or_blank(self):
        with mock.patch.dict(os.environ, {"FLAG": "  "}):
            self.assertIs(env_bool("FLAG", True), True)
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertIs(env_bool("FLAG", True), True)

    def test_env_bool_rejects_typos(self):
        with mock.patch.dict(os.environ, {"FLAG": "ture"}):
            with self.assertRaisesMessage(ImproperlyConfigured, "FLAG must be one of"):
                env_bool("FLAG")

    def test_env_int_parses_and_rejects_non_numbers(self):
        with mock.patch.dict(os.environ, {"NUM": " 42 "}):
            self.assertEqual(env_int("NUM", 1), 42)
        with mock.patch.dict(os.environ, {"NUM": "sixty"}):
            with self.assertRaisesMessage(ImproperlyConfigured, "NUM must be an integer"):
                env_int("NUM", 1)

    def test_env_list_trims_and_skips_empty_items(self):
        with mock.patch.dict(os.environ, {"ITEMS": " a.com, b.com ,, "}):
            self.assertEqual(env_list("ITEMS"), ["a.com", "b.com"])

    def test_env_str_required(self):
        with mock.patch.dict(os.environ, {"NAME": "  "}):
            with self.assertRaisesMessage(ImproperlyConfigured, "NAME must be set."):
                env_str("NAME", required=True)


class SettingsLoadTests(SimpleTestCase):
    def assertFailsWith(self, env, message):
        result = load_settings(env)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(message, result.stderr)

    def load(self, env, *names):
        result = load_settings(env, *names)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_requires_secret_key(self):
        env = {**VALID_ENV}
        del env["DJANGO_SECRET_KEY"]
        self.assertFailsWith(env, "DJANGO_SECRET_KEY must be set.")

    def test_requires_database_url(self):
        env = {**VALID_ENV}
        del env["DATABASE_URL"]
        self.assertFailsWith(env, "DATABASE_URL must be set.")

    def test_rejects_non_postgresql_database(self):
        self.assertFailsWith(
            {**VALID_ENV, "DATABASE_URL": "sqlite:///tmp/db.sqlite3"},
            "DATABASE_URL must point to a PostgreSQL database.",
        )

    def test_rejects_invalid_boolean(self):
        self.assertFailsWith({**VALID_ENV, "DJANGO_SECURE_SSL_REDIRECT": "ture"},
                             "DJANGO_SECURE_SSL_REDIRECT must be one of")

    def test_rejects_invalid_integer(self):
        self.assertFailsWith({**VALID_ENV, "DATABASE_CONN_MAX_AGE": "sixty"},
                             "DATABASE_CONN_MAX_AGE must be an integer")

    def test_production_defaults_are_secure(self):
        values = self.load(
            VALID_ENV,
            "DEBUG", "SECURE_SSL_REDIRECT", "SECURE_REDIRECT_EXEMPT", "SECURE_PROXY_SSL_HEADER",
            "SESSION_COOKIE_SECURE", "CSRF_COOKIE_SECURE", "SECURE_HSTS_SECONDS",
            "CORS_ALLOWED_ORIGINS", "CORS_ALLOW_CREDENTIALS", "REST_FRAMEWORK",
        )
        self.assertIs(values["DEBUG"], False)
        self.assertIs(values["SECURE_SSL_REDIRECT"], True)
        self.assertEqual(values["SECURE_REDIRECT_EXEMPT"], [r"^api/health/$"])
        self.assertEqual(values["SECURE_PROXY_SSL_HEADER"], ["HTTP_X_FORWARDED_PROTO", "https"])
        self.assertIs(values["SESSION_COOKIE_SECURE"], True)
        self.assertIs(values["CSRF_COOKIE_SECURE"], True)
        self.assertEqual(values["SECURE_HSTS_SECONDS"], 3600)
        self.assertEqual(values["CORS_ALLOWED_ORIGINS"], [])
        self.assertIs(values["CORS_ALLOW_CREDENTIALS"], False)
        self.assertEqual(
            values["REST_FRAMEWORK"]["DEFAULT_RENDERER_CLASSES"],
            ["rest_framework.renderers.JSONRenderer"],
        )

    def test_debug_mode_skips_https_enforcement(self):
        values = self.load({**VALID_ENV, "DJANGO_DEBUG": "true"}, "DEBUG", "SECURE_SSL_REDIRECT")
        self.assertIs(values["DEBUG"], True)
        self.assertIs(values["SECURE_SSL_REDIRECT"], False)

    def test_adds_render_hostname_to_allowed_hosts(self):
        values = self.load(
            {**VALID_ENV, "DJANGO_ALLOWED_HOSTS": "api.example.com",
             "RENDER_EXTERNAL_HOSTNAME": "kitchenmind-api.onrender.com"},
            "ALLOWED_HOSTS",
        )
        self.assertEqual(values["ALLOWED_HOSTS"], ["api.example.com", "kitchenmind-api.onrender.com"])

    def test_database_settings(self):
        values = self.load(VALID_ENV, "DATABASES")
        db = values["DATABASES"]["default"]
        self.assertEqual(db["ENGINE"], "django.db.backends.postgresql")
        self.assertEqual(db["CONN_MAX_AGE"], 60)
        self.assertIs(db["CONN_HEALTH_CHECKS"], True)
        self.assertEqual(db["OPTIONS"]["connect_timeout"], 5)

    def test_jwt_defaults_are_short_lived_and_rotating(self):
        jwt = self.load(VALID_ENV, "SIMPLE_JWT")["SIMPLE_JWT"]

        self.assertEqual(jwt["ACCESS_TOKEN_LIFETIME"], "0:15:00")
        self.assertEqual(jwt["REFRESH_TOKEN_LIFETIME"], "7 days, 0:00:00")
        self.assertIs(jwt["ROTATE_REFRESH_TOKENS"], True)
        self.assertIs(jwt["BLACKLIST_AFTER_ROTATION"], True)
        self.assertIs(jwt["CHECK_REVOKE_TOKEN"], True)
        self.assertEqual(jwt["ALGORITHM"], "HS256")
        self.assertEqual(jwt["SIGNING_KEY"], VALID_ENV["DJANGO_SECRET_KEY"])

    def test_jwt_lifetimes_and_proxy_count_are_configurable(self):
        values = self.load(
            {**VALID_ENV, "JWT_ACCESS_TOKEN_MINUTES": "5", "JWT_REFRESH_TOKEN_DAYS": "1",
             "DRF_NUM_PROXIES": "1"},
            "SIMPLE_JWT", "REST_FRAMEWORK",
        )

        self.assertEqual(values["SIMPLE_JWT"]["ACCESS_TOKEN_LIFETIME"], "0:05:00")
        self.assertEqual(values["SIMPLE_JWT"]["REFRESH_TOKEN_LIFETIME"], "1 day, 0:00:00")
        self.assertEqual(values["REST_FRAMEWORK"]["NUM_PROXIES"], 1)

    def test_database_options_are_configurable(self):
        values = self.load(
            {**VALID_ENV, "DATABASE_CONNECT_TIMEOUT": "2", "DATABASE_SSL_REQUIRE": "true"},
            "DATABASES",
        )
        options = values["DATABASES"]["default"]["OPTIONS"]
        self.assertEqual(options["connect_timeout"], 2)
        self.assertEqual(options["sslmode"], "require")


class DatabaseTests(TestCase):
    def test_uses_postgresql_17_or_newer(self):
        self.assertEqual(connection.vendor, "postgresql")
        self.assertGreaterEqual(connection.pg_version, 170000)

    def test_can_run_queries(self):
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            self.assertEqual(cursor.fetchone(), (1,))

    def test_connection_uses_utc(self):
        with connection.cursor() as cursor:
            cursor.execute("SHOW TIME ZONE")
            self.assertEqual(cursor.fetchone()[0], "UTC")

    def test_models_and_migrations_are_in_sync(self):
        # Exits with an error if any model change lacks a migration.
        call_command("makemigrations", check=True, dry_run=True, stdout=StringIO())


class ApiDefaultsTests(SimpleTestCase):
    def test_endpoints_require_authentication_unless_they_opt_out(self):
        self.assertEqual(api_settings.DEFAULT_PERMISSION_CLASSES, [IsAuthenticated])

    def test_authentication_uses_jwt_bearer_tokens_only(self):
        self.assertEqual(api_settings.DEFAULT_AUTHENTICATION_CLASSES, [JWTAuthentication])

    def test_sign_in_and_registration_are_rate_limited(self):
        self.assertEqual(api_settings.DEFAULT_THROTTLE_CLASSES, [ScopedRateThrottle])
        self.assertEqual(api_settings.DEFAULT_THROTTLE_RATES, {"auth": "10/minute"})

    def test_list_endpoints_are_paginated(self):
        self.assertEqual(api_settings.PAGE_SIZE, 50)
        self.assertEqual(
            api_settings.DEFAULT_PAGINATION_CLASS.__name__, "PageNumberPagination"
        )
