from django.contrib import admin
from django.contrib.auth import get_user_model
from django.test import TestCase

from .models import User


class UserModelTests(TestCase):
    def test_project_uses_custom_user_model(self):
        self.assertIs(get_user_model(), User)

    def test_create_user_hashes_password(self):
        user = User.objects.create_user(username="chef", email="chef@example.com", password="s3cret-pass")

        self.assertTrue(user.check_password("s3cret-pass"))
        self.assertNotEqual(user.password, "s3cret-pass")
        self.assertFalse(user.is_staff)

    def test_create_superuser(self):
        user = User.objects.create_superuser(username="owner", password="s3cret-pass")

        self.assertTrue(user.is_staff)
        self.assertTrue(user.is_superuser)

    def test_registered_in_admin(self):
        self.assertTrue(admin.site.is_registered(User))
