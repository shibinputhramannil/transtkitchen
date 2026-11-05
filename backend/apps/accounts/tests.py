from datetime import timedelta

import jwt
from django.contrib import admin
from django.contrib.auth import get_user_model
from django.core.exceptions import FieldDoesNotExist
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.urls import reverse
from rest_framework_simplejwt.tokens import AccessToken, RefreshToken

from apps.core.testing import APITestCase

from .models import User

PASSWORD = "Correct-Horse-42"


class UserModelTests(TestCase):
    def test_project_uses_custom_user_model(self):
        self.assertIs(get_user_model(), User)

    def test_users_sign_in_with_email_and_have_no_username(self):
        self.assertEqual(User.USERNAME_FIELD, "email")
        with self.assertRaises(FieldDoesNotExist):
            User._meta.get_field("username")

    def test_create_user_normalizes_email_and_hashes_password(self):
        user = User.objects.create_user(email="Chef@Example.COM", password=PASSWORD)

        self.assertEqual(user.email, "Chef@example.com")
        self.assertTrue(user.check_password(PASSWORD))
        self.assertNotEqual(user.password, PASSWORD)
        self.assertFalse(user.is_staff)
        self.assertFalse(user.is_superuser)

    def test_email_is_required(self):
        with self.assertRaises(ValueError):
            User.objects.create_user(email="", password=PASSWORD)

    def test_email_is_unique_ignoring_case(self):
        User.objects.create_user(email="chef@example.com", password=PASSWORD)

        with self.assertRaises(IntegrityError), transaction.atomic():
            User.objects.create_user(email="CHEF@example.com", password=PASSWORD)

    def test_natural_key_lookup_ignores_case(self):
        user = User.objects.create_user(email="chef@example.com", password=PASSWORD)

        self.assertEqual(User.objects.get_by_natural_key("Chef@Example.com"), user)

    def test_create_superuser(self):
        user = User.objects.create_superuser(email="owner@example.com", password=PASSWORD)

        self.assertTrue(user.is_staff)
        self.assertTrue(user.is_superuser)


class UserAdminTests(TestCase):
    def setUp(self):
        self.admin_user = User.objects.create_superuser(email="admin@example.com", password=PASSWORD)
        self.client.force_login(self.admin_user)

    def test_registered_in_admin(self):
        self.assertTrue(admin.site.is_registered(User))

    def test_admin_creates_user_by_email(self):
        response = self.client.post(
            reverse("admin:accounts_user_add"),
            {
                "email": "cook@example.com",
                "usable_password": "true",
                "password1": PASSWORD,
                "password2": PASSWORD,
            },
            secure=True,
        )

        self.assertEqual(response.status_code, 302)
        self.assertTrue(User.objects.get(email="cook@example.com").check_password(PASSWORD))

    def test_admin_rejects_duplicate_email_ignoring_case(self):
        response = self.client.post(
            reverse("admin:accounts_user_add"),
            {
                "email": "ADMIN@example.com",
                "usable_password": "true",
                "password1": PASSWORD,
                "password2": PASSWORD,
            },
            secure=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(User.objects.count(), 1)

    def test_admin_lists_and_edits_users(self):
        changelist = self.client.get(reverse("admin:accounts_user_changelist"), secure=True)
        change = self.client.get(
            reverse("admin:accounts_user_change", args=[self.admin_user.pk]), secure=True
        )

        self.assertEqual(changelist.status_code, 200)
        self.assertEqual(change.status_code, 200)


class RegisterTests(APITestCase):
    url = reverse("register")

    def test_creates_account_without_returning_the_password(self):
        response = self.client.post(
            self.url, {"email": "chef@example.com", "password": PASSWORD, "first_name": "Ana"}
        )

        self.assertEqual(response.status_code, 201)
        user = User.objects.get()
        self.assertEqual(
            response.json(),
            {"id": user.pk, "email": "chef@example.com", "first_name": "Ana", "last_name": ""},
        )
        self.assertTrue(user.check_password(PASSWORD))

    def test_rejects_duplicate_email_ignoring_case(self):
        User.objects.create_user(email="chef@example.com", password=PASSWORD)

        response = self.client.post(self.url, {"email": "Chef@Example.com", "password": PASSWORD})

        self.assertEqual(response.status_code, 400)
        self.assertIn("email", response.json())
        self.assertEqual(User.objects.count(), 1)

    def test_rejects_weak_password(self):
        response = self.client.post(self.url, {"email": "chef@example.com", "password": "password"})

        self.assertEqual(response.status_code, 400)
        self.assertIn("password", response.json())
        self.assertFalse(User.objects.exists())

    def test_requires_valid_email_and_password(self):
        response = self.client.post(self.url, {"email": "not-an-email"})

        self.assertEqual(response.status_code, 400)
        self.assertEqual(set(response.json()), {"email", "password"})

    def test_ignores_privilege_fields(self):
        self.client.post(
            self.url,
            {"email": "chef@example.com", "password": PASSWORD, "is_staff": True, "is_superuser": True},
        )

        user = User.objects.get()
        self.assertFalse(user.is_staff)
        self.assertFalse(user.is_superuser)

    def test_is_rate_limited(self):
        for attempt in range(10):
            self.client.post(self.url, {"email": f"user{attempt}@example.com", "password": "x"})

        response = self.client.post(self.url, {"email": "late@example.com", "password": PASSWORD})

        self.assertEqual(response.status_code, 429)


class LoginTests(APITestCase):
    url = reverse("token")

    def setUp(self):
        super().setUp()
        self.user = User.objects.create_user(email="chef@example.com", password=PASSWORD)

    def login(self, email, password):
        return self.client.post(self.url, {"email": email, "password": password})

    def test_returns_tokens_that_authenticate(self):
        response = self.login("chef@example.com", PASSWORD)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(set(response.json()), {"access", "refresh"})
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {response.json()['access']}")
        self.assertEqual(self.client.get(reverse("me")).json()["email"], "chef@example.com")

    def test_email_is_case_insensitive(self):
        self.assertEqual(self.login("CHEF@example.com", PASSWORD).status_code, 200)

    def test_wrong_password_and_unknown_email_look_the_same(self):
        wrong_password = self.login("chef@example.com", "wrong-password")
        unknown_email = self.login("nobody@example.com", "wrong-password")

        self.assertEqual(wrong_password.status_code, 401)
        self.assertEqual(unknown_email.status_code, 401)
        self.assertEqual(wrong_password.json(), unknown_email.json())

    def test_inactive_user_cannot_sign_in(self):
        self.user.is_active = False
        self.user.save()

        self.assertEqual(self.login("chef@example.com", PASSWORD).status_code, 401)

    def test_records_last_login(self):
        self.login("chef@example.com", PASSWORD)

        self.user.refresh_from_db()
        self.assertIsNotNone(self.user.last_login)

    def test_is_rate_limited(self):
        for _ in range(10):
            self.login("chef@example.com", "wrong-password")

        self.assertEqual(self.login("chef@example.com", PASSWORD).status_code, 429)


class TokenAuthenticationTests(APITestCase):
    url = reverse("me")

    def setUp(self):
        super().setUp()
        self.user = User.objects.create_user(email="chef@example.com", password=PASSWORD)

    def get_with_token(self, token):
        return self.client.get(self.url, HTTP_AUTHORIZATION=f"Bearer {token}")

    def test_requires_a_token(self):
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 401)
        self.assertTrue(response["WWW-Authenticate"].startswith("Bearer"))

    def test_accepts_a_valid_access_token(self):
        self.assertEqual(self.get_with_token(AccessToken.for_user(self.user)).status_code, 200)

    def test_rejects_malformed_token(self):
        self.assertEqual(self.get_with_token("not-a-token").status_code, 401)

    def test_rejects_expired_token(self):
        token = AccessToken.for_user(self.user)
        token.set_exp(lifetime=-timedelta(seconds=1))

        self.assertEqual(self.get_with_token(token).status_code, 401)

    def test_rejects_token_signed_with_another_key(self):
        payload = AccessToken.for_user(self.user).payload

        self.assertEqual(
            self.get_with_token(
                jwt.encode(payload, "another-signing-key-of-sufficient-length", algorithm="HS256")
            ).status_code,
            401,
        )

    def test_rejects_unsigned_token(self):
        payload = AccessToken.for_user(self.user).payload

        self.assertEqual(
            self.get_with_token(jwt.encode(payload, None, algorithm="none")).status_code, 401
        )

    def test_rejects_refresh_token_used_as_access_token(self):
        self.assertEqual(self.get_with_token(RefreshToken.for_user(self.user)).status_code, 401)

    def test_rejects_tokens_of_deactivated_user(self):
        token = AccessToken.for_user(self.user)
        self.user.is_active = False
        self.user.save()

        self.assertEqual(self.get_with_token(token).status_code, 401)


class RefreshAndLogoutTests(APITestCase):
    refresh_url = reverse("token-refresh")
    logout_url = reverse("logout")

    def setUp(self):
        super().setUp()
        self.user = User.objects.create_user(email="chef@example.com", password=PASSWORD)
        self.refresh = str(RefreshToken.for_user(self.user))

    def refresh_with(self, token):
        return self.client.post(self.refresh_url, {"refresh": token})

    def test_refresh_rotates_and_revokes_the_old_token(self):
        response = self.refresh_with(self.refresh)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(set(response.json()), {"access", "refresh"})
        self.assertEqual(self.refresh_with(self.refresh).status_code, 401)
        self.assertEqual(self.refresh_with(response.json()["refresh"]).status_code, 200)

    def test_logout_revokes_the_refresh_token(self):
        self.assertEqual(self.client.post(self.logout_url, {"refresh": self.refresh}).status_code, 200)

        self.assertEqual(self.refresh_with(self.refresh).status_code, 401)

    def test_logout_rejects_invalid_token(self):
        self.assertEqual(self.client.post(self.logout_url, {"refresh": "nope"}).status_code, 401)

    def test_password_change_revokes_existing_tokens(self):
        access = AccessToken.for_user(self.user)
        self.user.set_password("Another-Horse-43")
        self.user.save()

        self.assertEqual(
            self.client.get(reverse("me"), HTTP_AUTHORIZATION=f"Bearer {access}").status_code, 401
        )
        self.assertEqual(self.refresh_with(self.refresh).status_code, 401)

    def test_refresh_is_rejected_for_deactivated_user(self):
        self.user.is_active = False
        self.user.save()

        self.assertEqual(self.refresh_with(self.refresh).status_code, 401)

    def test_refresh_is_rejected_for_deleted_user(self):
        self.user.delete()

        self.assertEqual(self.refresh_with(self.refresh).status_code, 401)


class PasswordChangeTests(APITestCase):
    url = reverse("password-change")
    NEW = "Battery-Staple-77"

    def setUp(self):
        super().setUp()
        self.user = User.objects.create_user(email="chef@example.com", password=PASSWORD)
        self.other = User.objects.create_user(email="other@example.com", password=PASSWORD)
        self.refresh = RefreshToken.for_user(self.user)
        self.access = str(self.refresh.access_token)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {self.access}")

    def change(self, current=PASSWORD, new=None, **extra):
        return self.client.post(self.url, {"current_password": current, "new_password": new or self.NEW, **extra})

    def test_requires_sign_in(self):
        self.client.credentials()

        self.assertEqual(self.change().status_code, 401)

    def test_wrong_current_password_changes_nothing(self):
        response = self.change(current="not-my-password")

        self.assertEqual(response.status_code, 400)
        self.assertIn("current_password", response.json())
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(PASSWORD))

    def test_new_password_must_pass_the_password_rules(self):
        for new in ("password", "12345678", "chef@example", PASSWORD):
            with self.subTest(new=new):
                response = self.change(new=new)
                self.assertEqual(response.status_code, 400)
                self.assertIn("new_password", response.json())
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(PASSWORD))

    def test_success_revokes_old_tokens_and_returns_working_ones(self):
        response = self.change()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(set(response.json()), {"access", "refresh"})
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(self.NEW))
        # Every earlier token, on any device, stops working.
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {self.access}")
        self.assertEqual(self.client.get(reverse("me")).status_code, 401)
        self.assertEqual(self.client.post(reverse("token-refresh"), {"refresh": str(self.refresh)}).status_code, 401)
        # The returned pair works.
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {response.json()['access']}")
        self.assertEqual(self.client.get(reverse("me")).json()["email"], "chef@example.com")
        self.assertEqual(self.client.post(reverse("token-refresh"), {"refresh": response.json()["refresh"]}).status_code, 200)
        # The new password signs in.
        self.client.credentials()
        self.assertEqual(self.client.post(reverse("token"), {"email": "chef@example.com", "password": self.NEW}).status_code, 200)

    def test_only_the_callers_password_changes(self):
        response = self.change(email="other@example.com", user=self.other.pk, id=self.other.pk)

        self.assertEqual(response.status_code, 200)
        self.other.refresh_from_db()
        self.assertTrue(self.other.check_password(PASSWORD))
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(self.NEW))

    def test_is_rate_limited(self):
        for _ in range(10):
            self.change(current="wrong-guess")

        self.assertEqual(self.change().status_code, 429)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(PASSWORD))


class MeTests(APITestCase):
    url = reverse("me")

    def setUp(self):
        super().setUp()
        self.user = User.objects.create_user(
            email="chef@example.com", password=PASSWORD, first_name="Ana"
        )
        self.authenticate(self.user)

    def test_returns_profile(self):
        self.assertEqual(
            self.client.get(self.url).json(),
            {"id": self.user.pk, "email": "chef@example.com", "first_name": "Ana", "last_name": ""},
        )

    def test_updates_names_only(self):
        response = self.client.patch(
            self.url,
            {"first_name": "Bea", "email": "other@example.com", "is_staff": True},
        )

        self.assertEqual(response.status_code, 200)
        self.user.refresh_from_db()
        self.assertEqual(self.user.first_name, "Bea")
        self.assertEqual(self.user.email, "chef@example.com")
        self.assertFalse(self.user.is_staff)

    def test_put_is_not_allowed(self):
        self.assertEqual(self.client.put(self.url, {"first_name": "Bea"}).status_code, 405)
