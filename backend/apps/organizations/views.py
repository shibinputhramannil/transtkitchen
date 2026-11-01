from django.db import IntegrityError, transaction
from django.db.models import F
from django.http import Http404
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import generics, status
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.permissions import SAFE_METHODS
from rest_framework.response import Response
from rest_framework.views import APIView

from .mixins import OrganizationScopedMixin
from .models import Invitation, Membership, Organization, Role
from .permissions import can_manage
from .serializers import (
    InvitationSerializer,
    MembershipSerializer,
    MyInvitationSerializer,
    OrganizationSerializer,
)

LAST_OWNER = "An organization must keep at least one owner."
DUPLICATE_INVITATION = "This email address already has a pending invitation."


class OrganizationListCreateView(generics.ListCreateAPIView):
    """Lists the organizations the requester belongs to; creating one makes them its owner."""

    serializer_class = OrganizationSerializer

    def get_queryset(self):
        # Filtering before annotating keeps the role to the requester's own membership.
        return Organization.objects.filter(memberships__user=self.request.user).annotate(
            role=F("memberships__role")
        )

    def perform_create(self, serializer):
        with transaction.atomic():
            organization = serializer.save()
            Membership.objects.create(
                user=self.request.user, organization=organization, role=Role.OWNER
            )
        organization.role = Role.OWNER


class OrganizationDetailView(OrganizationScopedMixin, generics.RetrieveUpdateAPIView):
    serializer_class = OrganizationSerializer
    http_method_names = ["get", "patch", "head", "options"]

    def get_minimum_role(self):
        return Role.STAFF if self.request.method in SAFE_METHODS else Role.OWNER

    def get_object(self):
        organization = self.organization
        organization.role = self.membership.role
        return organization


class MembershipListView(OrganizationScopedMixin, generics.ListAPIView):
    serializer_class = MembershipSerializer

    def get_queryset(self):
        return self.organization.memberships.select_related("user")


class MembershipDetailView(OrganizationScopedMixin, generics.RetrieveUpdateDestroyAPIView):
    """
    Owners change roles and remove anyone; managers remove staff; every member
    can remove themselves. The organization always keeps at least one owner.
    """

    serializer_class = MembershipSerializer
    http_method_names = ["get", "patch", "delete", "head", "options"]

    def get_queryset(self):
        return self.organization.memberships.select_related("user")

    def lock_organization(self):
        # Concurrent role changes in one organization run one at a time, and
        # are checked against the requester's current role, so two owners
        # cannot demote each other at once and leave no owner.
        Organization.objects.select_for_update().get(pk=self.organization.pk)
        try:
            self.membership.refresh_from_db(fields=["role"])
        except Membership.DoesNotExist:
            raise Http404 from None

    def ensure_another_owner(self, membership):
        others = self.organization.memberships.filter(role=Role.OWNER).exclude(pk=membership.pk)
        if not others.exists():
            raise ValidationError({"detail": LAST_OWNER})

    @transaction.atomic
    def update(self, request, *args, **kwargs):
        self.lock_organization()
        return super().update(request, *args, **kwargs)

    @transaction.atomic
    def destroy(self, request, *args, **kwargs):
        self.lock_organization()
        return super().destroy(request, *args, **kwargs)

    def perform_update(self, serializer):
        if self.membership.role != Role.OWNER:
            raise PermissionDenied("Only owners can change roles.")
        target = serializer.instance
        if target.role == Role.OWNER and serializer.validated_data.get("role", Role.OWNER) != Role.OWNER:
            self.ensure_another_owner(target)
        serializer.save()

    def perform_destroy(self, instance):
        is_self = instance.pk == self.membership.pk
        if not (is_self or can_manage(self.membership.role, instance.role)):
            raise PermissionDenied("You cannot remove this member.")
        if instance.role == Role.OWNER:
            self.ensure_another_owner(instance)
        instance.delete()


class OrganizationInvitationListCreateView(OrganizationScopedMixin, generics.ListCreateAPIView):
    """
    Invites an email address to the organization. The response is the same
    whether or not the address has an account; invitees accept after signing in.
    """

    serializer_class = InvitationSerializer
    minimum_role = Role.MANAGER

    def get_queryset(self):
        return self.organization.invitations.pending().select_related("invited_by")

    def perform_create(self, serializer):
        email = serializer.validated_data["email"]
        role = serializer.validated_data.get("role", Role.STAFF)
        if not can_manage(self.membership.role, role):
            raise PermissionDenied("Managers can only invite staff.")
        # Members are already visible to this organization, so saying so reveals nothing new.
        if self.organization.memberships.filter(user__email__iexact=email).exists():
            raise ValidationError({"email": ["This person is already a member."]})

        try:
            with transaction.atomic():
                # An expired invitation must not block a new one; a pending one does,
                # through the unique constraint.
                self.organization.invitations.for_email(email).filter(
                    expires_at__lte=timezone.now()
                ).delete()
                serializer.save(organization=self.organization, invited_by=self.request.user)
        except IntegrityError:
            raise ValidationError({"email": [DUPLICATE_INVITATION]}) from None


class OrganizationInvitationDetailView(OrganizationScopedMixin, generics.DestroyAPIView):
    """Revokes an invitation."""

    minimum_role = Role.MANAGER

    def get_queryset(self):
        return self.organization.invitations.all()

    def perform_destroy(self, instance):
        if not can_manage(self.membership.role, instance.role):
            raise PermissionDenied("Managers can only revoke invitations for staff.")
        instance.delete()


def invitations_for(user):
    return Invitation.objects.pending().for_email(user.email)


class MyInvitationListView(generics.ListAPIView):
    """Pending invitations addressed to the requester's email address."""

    serializer_class = MyInvitationSerializer

    def get_queryset(self):
        return invitations_for(self.request.user).select_related("organization", "invited_by")


class AcceptInvitationView(APIView):
    def post(self, request, pk):
        with transaction.atomic():
            invitation = get_object_or_404(
                invitations_for(request.user).select_for_update(), pk=pk
            )
            membership, created = Membership.objects.get_or_create(
                user=request.user,
                organization_id=invitation.organization_id,
                defaults={"role": invitation.role},
            )
            invitation.delete()

        # An existing member keeps their current role.
        organization = membership.organization
        organization.role = membership.role
        return Response(
            OrganizationSerializer(organization).data,
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )


class DeclineInvitationView(APIView):
    def post(self, request, pk):
        get_object_or_404(invitations_for(request.user), pk=pk).delete()
        return Response(status=status.HTTP_204_NO_CONTENT)
