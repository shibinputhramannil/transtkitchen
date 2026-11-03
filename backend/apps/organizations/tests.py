import uuid
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User

from .models import Invitation, Membership, Organization, Role
from .testing import PASSWORD, TenantTestCase, make_user, org_url


class MembershipModelTests(TestCase):
    def test_user_belongs_to_an_organization_once(self):
        user = make_user("chef@example.com")
        organization = Organization.objects.create(name="Bakery")
        Membership.objects.create(user=user, organization=organization)

        with self.assertRaises(IntegrityError), transaction.atomic():
            Membership.objects.create(user=user, organization=organization, role=Role.OWNER)

    def test_database_rejects_unknown_roles(self):
        user = make_user("chef@example.com")
        organization = Organization.objects.create(name="Bakery")

        with self.assertRaises(IntegrityError), transaction.atomic():
            Membership.objects.create(user=user, organization=organization, role="admin")

    def test_one_pending_invitation_per_email_ignoring_case(self):
        organization = Organization.objects.create(name="Bakery")
        Invitation.objects.create(organization=organization, email="cook@example.com")

        with self.assertRaises(IntegrityError), transaction.atomic():
            Invitation.objects.create(organization=organization, email="COOK@example.com")

    def test_invitations_expire_after_seven_days(self):
        invitation = Invitation.objects.create(
            organization=Organization.objects.create(name="Bakery"), email="cook@example.com"
        )

        self.assertAlmostEqual(
            invitation.expires_at, timezone.now() + timedelta(days=7), delta=timedelta(minutes=1)
        )


class OrganizationListCreateTests(TenantTestCase):
    url = reverse("organization-list")

    def test_requires_authentication(self):
        self.assertEqual(self.client.get(self.url).status_code, 401)
        self.assertEqual(self.client.post(self.url, {"name": "X"}).status_code, 401)

    def test_creator_becomes_owner(self):
        self.authenticate(self.outsider)

        response = self.client.post(self.url, {"name": "Cloud Kitchen"})

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["name"], "Cloud Kitchen")
        self.assertEqual(response.json()["role"], "owner")
        membership = Membership.objects.get(user=self.outsider)
        self.assertEqual(str(membership.organization_id), response.json()["id"])
        self.assertEqual(membership.role, Role.OWNER)

    def test_rejects_blank_name(self):
        self.authenticate(self.outsider)

        self.assertEqual(self.client.post(self.url, {"name": "  "}).status_code, 400)
        self.assertFalse(Organization.objects.filter(memberships__user=self.outsider).exists())

    def test_lists_only_own_organizations_with_own_role(self):
        Membership.objects.create(user=self.staff_a, organization=self.org_b, role=Role.MANAGER)
        self.authenticate(self.staff_a)

        results = self.client.get(self.url).json()["results"]

        self.assertEqual(
            [(item["name"], item["role"]) for item in results],
            [("Bakery A", "staff"), ("Kitchen B", "manager")],
        )

    def test_outsider_sees_nothing(self):
        self.authenticate(self.outsider)

        self.assertEqual(self.client.get(self.url).json()["count"], 0)


class OrganizationDetailTests(TenantTestCase):
    def test_members_see_the_organization_with_their_role(self):
        for user, role in [(self.owner_a, "owner"), (self.manager_a, "manager"), (self.staff_a, "staff")]:
            with self.subTest(role=role):
                self.authenticate(user)
                response = self.client.get(org_url("organization-detail", self.org_a))

                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["role"], role)

    def test_other_tenants_get_the_same_404_as_a_missing_organization(self):
        self.authenticate(self.owner_b)

        other = self.client.get(org_url("organization-detail", self.org_a))
        missing = self.client.get(
            reverse("organization-detail", kwargs={"organization_id": uuid.uuid4()})
        )

        self.assertEqual(other.status_code, 404)
        self.assertEqual(other.json(), missing.json())

    def test_superuser_without_membership_gets_404(self):
        admin = User.objects.create_superuser(email="admin@example.com", password=PASSWORD)
        self.authenticate(admin)

        self.assertEqual(self.client.get(org_url("organization-detail", self.org_a)).status_code, 404)

    def test_only_owners_rename(self):
        url = org_url("organization-detail", self.org_a)
        for user, expected in [
            (self.staff_a, 403),
            (self.manager_a, 403),
            (self.owner_b, 404),
            (self.owner_a, 200),
        ]:
            with self.subTest(user=user.email):
                self.authenticate(user)
                self.assertEqual(self.client.patch(url, {"name": "Renamed"}).status_code, expected)

        self.org_a.refresh_from_db()
        self.assertEqual(self.org_a.name, "Renamed")

    def test_delete_is_not_available(self):
        self.authenticate(self.owner_a)

        self.assertEqual(self.client.delete(org_url("organization-detail", self.org_a)).status_code, 405)


class MembershipTests(TenantTestCase):
    def member_url(self, membership, organization=None):
        return org_url(
            "membership-detail", organization or membership.organization, pk=membership.pk
        )

    def test_any_member_lists_members(self):
        self.authenticate(self.staff_a)

        results = self.client.get(org_url("membership-list", self.org_a)).json()["results"]

        self.assertEqual(
            {(item["user"]["email"], item["role"]) for item in results},
            {("owner@a.example", "owner"), ("manager@a.example", "manager"), ("staff@a.example", "staff")},
        )

    def test_other_tenants_cannot_list_members(self):
        self.authenticate(self.owner_b)

        self.assertEqual(self.client.get(org_url("membership-list", self.org_a)).status_code, 404)

    def test_only_owners_change_roles(self):
        url = self.member_url(self.staff_a_membership)
        for user, expected in [(self.staff_a, 403), (self.manager_a, 403), (self.owner_a, 200)]:
            with self.subTest(user=user.email):
                self.authenticate(user)
                self.assertEqual(self.client.patch(url, {"role": "manager"}).status_code, expected)

        self.staff_a_membership.refresh_from_db()
        self.assertEqual(self.staff_a_membership.role, Role.MANAGER)

    def test_rejects_unknown_role(self):
        self.authenticate(self.owner_a)

        response = self.client.patch(self.member_url(self.staff_a_membership), {"role": "admin"})

        self.assertEqual(response.status_code, 400)

    def test_last_owner_cannot_be_demoted(self):
        self.authenticate(self.owner_a)

        response = self.client.patch(self.member_url(self.owner_a_membership), {"role": "manager"})

        self.assertEqual(response.status_code, 400)
        self.owner_a_membership.refresh_from_db()
        self.assertEqual(self.owner_a_membership.role, Role.OWNER)

    def test_owner_can_step_down_when_another_owner_exists(self):
        self.manager_a_membership.role = Role.OWNER
        self.manager_a_membership.save()
        self.authenticate(self.owner_a)

        response = self.client.patch(self.member_url(self.owner_a_membership), {"role": "staff"})

        self.assertEqual(response.status_code, 200)

    def test_removal_rules(self):
        cases = [
            (self.staff_a, self.manager_a_membership, 403),
            (self.manager_a, self.owner_a_membership, 403),
            (self.manager_a, self.staff_a_membership, 204),
            (self.owner_a, self.manager_a_membership, 204),
        ]
        for user, membership, expected in cases:
            with self.subTest(user=user.email, target=membership.user.email):
                self.authenticate(user)
                self.assertEqual(self.client.delete(self.member_url(membership)).status_code, expected)

        self.assertEqual(
            list(self.org_a.memberships.values_list("user__email", flat=True)), ["owner@a.example"]
        )

    def test_members_can_leave(self):
        self.authenticate(self.staff_a)

        self.assertEqual(self.client.delete(self.member_url(self.staff_a_membership)).status_code, 204)

    def test_last_owner_cannot_leave(self):
        self.authenticate(self.owner_a)

        self.assertEqual(self.client.delete(self.member_url(self.owner_a_membership)).status_code, 400)
        self.assertTrue(Membership.objects.filter(pk=self.owner_a_membership.pk).exists())

    def test_cannot_reach_another_tenants_membership_through_own_organization(self):
        self.authenticate(self.owner_a)
        url = self.member_url(self.owner_b_membership, organization=self.org_a)

        self.assertEqual(self.client.get(url).status_code, 404)
        self.assertEqual(self.client.patch(url, {"role": "staff"}).status_code, 404)
        self.assertEqual(self.client.delete(url).status_code, 404)
        self.owner_b_membership.refresh_from_db()
        self.assertEqual(self.owner_b_membership.role, Role.OWNER)

    def test_cannot_reach_another_tenants_membership_directly(self):
        self.authenticate(self.owner_a)
        url = self.member_url(self.owner_b_membership)

        self.assertEqual(self.client.patch(url, {"role": "staff"}).status_code, 404)
        self.assertEqual(self.client.delete(url).status_code, 404)

    def test_removed_member_loses_access_immediately(self):
        self.authenticate(self.staff_a)
        self.staff_a_membership.delete()

        self.assertEqual(self.client.get(org_url("organization-detail", self.org_a)).status_code, 404)

    def test_demotion_takes_effect_immediately(self):
        self.authenticate(self.manager_a)
        self.manager_a_membership.role = Role.STAFF
        self.manager_a_membership.save()

        response = self.client.post(
            org_url("organization-invitation-list", self.org_a), {"email": "new@example.com"}
        )

        self.assertEqual(response.status_code, 403)


class OrganizationInvitationTests(TenantTestCase):
    def invite(self, user, email, role=None, organization=None):
        self.authenticate(user)
        data = {"email": email}
        if role:
            data["role"] = role
        return self.client.post(
            org_url("organization-invitation-list", organization or self.org_a), data
        )

    def test_response_does_not_reveal_whether_the_email_has_an_account(self):
        registered = self.invite(self.owner_a, "outsider@example.com")
        unregistered = self.invite(self.owner_a, "nobody@example.com")

        self.assertEqual(registered.status_code, 201)
        self.assertEqual(unregistered.status_code, 201)
        self.assertEqual(set(registered.json()), set(unregistered.json()))
        self.assertEqual(registered.json()["role"], unregistered.json()["role"])
        # Nobody joins until they accept.
        self.assertFalse(Membership.objects.filter(user=self.outsider).exists())
        listed = self.client.get(org_url("organization-invitation-list", self.org_a)).json()
        self.assertEqual(
            sorted(item["email"] for item in listed["results"]),
            ["nobody@example.com", "outsider@example.com"],
        )

    def test_who_can_invite_which_roles(self):
        cases = [
            (self.staff_a, "staff", 403),
            (self.manager_a, "manager", 403),
            (self.manager_a, "owner", 403),
            (self.manager_a, "staff", 201),
            (self.owner_a, "manager", 201),
            (self.owner_b, "staff", 404),
        ]
        for number, (user, role, expected) in enumerate(cases):
            with self.subTest(user=user.email, role=role):
                response = self.invite(user, f"person{number}@example.com", role=role)
                self.assertEqual(response.status_code, expected)

    def test_defaults_to_staff(self):
        self.assertEqual(self.invite(self.owner_a, "cook@example.com").json()["role"], "staff")

    def test_rejects_existing_member(self):
        response = self.invite(self.owner_a, "STAFF@a.example")

        self.assertEqual(response.status_code, 400)

    def test_rejects_duplicate_pending_invitation_ignoring_case(self):
        self.invite(self.owner_a, "cook@example.com")

        self.assertEqual(self.invite(self.owner_a, "Cook@Example.com").status_code, 400)
        self.assertEqual(self.org_a.invitations.count(), 1)

    def test_expired_invitation_can_be_replaced(self):
        Invitation.objects.create(
            organization=self.org_a,
            email="cook@example.com",
            expires_at=timezone.now() - timedelta(seconds=1),
        )

        self.assertEqual(self.invite(self.owner_a, "cook@example.com").status_code, 201)
        self.assertEqual(self.org_a.invitations.count(), 1)

    def test_organization_comes_from_the_url_not_the_body(self):
        self.authenticate(self.owner_a)
        response = self.client.post(
            org_url("organization-invitation-list", self.org_a),
            {"email": "cook@example.com", "organization": str(self.org_b.pk), "organization_id": str(self.org_b.pk)},
        )

        self.assertEqual(response.status_code, 201)
        self.assertTrue(self.org_a.invitations.exists())
        self.assertFalse(self.org_b.invitations.exists())

    def test_staff_cannot_list_invitations(self):
        self.authenticate(self.staff_a)

        self.assertEqual(
            self.client.get(org_url("organization-invitation-list", self.org_a)).status_code, 403
        )

    def test_expired_invitations_are_not_listed(self):
        Invitation.objects.create(
            organization=self.org_a,
            email="old@example.com",
            expires_at=timezone.now() - timedelta(seconds=1),
        )
        self.authenticate(self.owner_a)

        listed = self.client.get(org_url("organization-invitation-list", self.org_a)).json()

        self.assertEqual(listed["count"], 0)

    def test_revoke_rules(self):
        staff_invite = Invitation.objects.create(organization=self.org_a, email="s@example.com")
        manager_invite = Invitation.objects.create(
            organization=self.org_a, email="m@example.com", role=Role.MANAGER
        )
        cases = [
            (self.staff_a, staff_invite, 403),
            (self.manager_a, manager_invite, 403),
            (self.owner_b, staff_invite, 404),
            (self.manager_a, staff_invite, 204),
            (self.owner_a, manager_invite, 204),
        ]
        for user, invitation, expected in cases:
            with self.subTest(user=user.email, invitation=invitation.email):
                self.authenticate(user)
                url = org_url("organization-invitation-detail", self.org_a, pk=invitation.pk)
                self.assertEqual(self.client.delete(url).status_code, expected)

        self.assertFalse(self.org_a.invitations.exists())

    def test_cannot_revoke_another_tenants_invitation_through_own_organization(self):
        invitation = Invitation.objects.create(organization=self.org_b, email="cook@example.com")
        self.authenticate(self.owner_a)

        url = org_url("organization-invitation-detail", self.org_a, pk=invitation.pk)

        self.assertEqual(self.client.delete(url).status_code, 404)
        self.assertTrue(Invitation.objects.filter(pk=invitation.pk).exists())


class MyInvitationTests(TenantTestCase):
    def setUp(self):
        super().setUp()
        self.invitation = Invitation.objects.create(
            organization=self.org_a,
            email="Outsider@Example.com",
            role=Role.MANAGER,
            invited_by=self.owner_a,
        )

    def accept(self, invitation):
        return self.client.post(reverse("invitation-accept", kwargs={"pk": invitation.pk}))

    def test_invitee_sees_their_invitations_ignoring_case(self):
        self.authenticate(self.outsider)

        results = self.client.get(reverse("invitation-list")).json()["results"]

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["organization"], {"id": str(self.org_a.pk), "name": "Bakery A"})
        self.assertEqual(results[0]["role"], "manager")
        self.assertEqual(results[0]["invited_by"], "owner@a.example")

    def test_other_users_do_not_see_it(self):
        self.authenticate(self.owner_b)

        self.assertEqual(self.client.get(reverse("invitation-list")).json()["count"], 0)

    def test_accepting_joins_with_the_invited_role(self):
        self.authenticate(self.outsider)

        response = self.accept(self.invitation)

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["role"], "manager")
        self.assertEqual(Membership.objects.get(user=self.outsider).role, Role.MANAGER)
        self.assertFalse(Invitation.objects.exists())
        self.assertEqual(self.client.get(org_url("organization-detail", self.org_a)).status_code, 200)

    def test_cannot_accept_someone_elses_invitation(self):
        self.authenticate(self.owner_b)

        self.assertEqual(self.accept(self.invitation).status_code, 404)
        self.assertFalse(Membership.objects.filter(user=self.owner_b, organization=self.org_a).exists())

    def test_cannot_accept_expired_invitation(self):
        self.invitation.expires_at = timezone.now() - timedelta(seconds=1)
        self.invitation.save()
        self.authenticate(self.outsider)

        self.assertEqual(self.accept(self.invitation).status_code, 404)

    def test_existing_member_keeps_their_role(self):
        invitation = Invitation.objects.create(
            organization=self.org_a, email="staff@a.example", role=Role.OWNER
        )
        self.authenticate(self.staff_a)

        response = self.accept(invitation)

        self.assertEqual(response.status_code, 200)
        self.staff_a_membership.refresh_from_db()
        self.assertEqual(self.staff_a_membership.role, Role.STAFF)

    def test_declining_deletes_the_invitation(self):
        self.authenticate(self.outsider)

        response = self.client.post(reverse("invitation-decline", kwargs={"pk": self.invitation.pk}))

        self.assertEqual(response.status_code, 204)
        self.assertFalse(Invitation.objects.exists())
        self.assertFalse(Membership.objects.filter(user=self.outsider).exists())

    def test_someone_who_registers_later_can_accept(self):
        invitation = Invitation.objects.create(organization=self.org_a, email="new@example.com")
        self.client.post(reverse("register"), {"email": "new@example.com", "password": PASSWORD})
        self.authenticate(User.objects.get(email="new@example.com"))

        self.assertEqual(self.accept(invitation).status_code, 201)

    def test_requires_authentication(self):
        self.assertEqual(self.client.get(reverse("invitation-list")).status_code, 401)
        self.assertEqual(self.accept(self.invitation).status_code, 401)
