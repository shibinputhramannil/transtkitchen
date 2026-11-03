"""Shared fixtures for tests of organization-scoped APIs."""

from django.urls import reverse

from apps.accounts.models import User
from apps.core.testing import APITestCase

from .models import Membership, Organization, Role

PASSWORD = "Correct-Horse-42"


def make_user(email):
    return User.objects.create_user(email=email, password=PASSWORD)


def org_url(name, organization, **kwargs):
    return reverse(name, kwargs={"organization_id": organization.pk, **kwargs})


class TenantTestCase(APITestCase):
    """Two organizations, A and B, with separate people, plus an outsider."""

    @classmethod
    def setUpTestData(cls):
        cls.org_a = Organization.objects.create(name="Bakery A")
        cls.org_b = Organization.objects.create(name="Kitchen B")
        cls.owner_a = make_user("owner@a.example")
        cls.manager_a = make_user("manager@a.example")
        cls.staff_a = make_user("staff@a.example")
        cls.owner_b = make_user("owner@b.example")
        cls.outsider = make_user("outsider@example.com")
        cls.owner_a_membership = Membership.objects.create(
            user=cls.owner_a, organization=cls.org_a, role=Role.OWNER
        )
        cls.manager_a_membership = Membership.objects.create(
            user=cls.manager_a, organization=cls.org_a, role=Role.MANAGER
        )
        cls.staff_a_membership = Membership.objects.create(
            user=cls.staff_a, organization=cls.org_a, role=Role.STAFF
        )
        cls.owner_b_membership = Membership.objects.create(
            user=cls.owner_b, organization=cls.org_b, role=Role.OWNER
        )
