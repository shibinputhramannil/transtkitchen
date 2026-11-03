from django.contrib.auth.base_user import BaseUserManager
from rest_framework import serializers

from apps.accounts.serializers import UserSerializer

from .models import Invitation, Membership, Organization


class OrganizationSerializer(serializers.ModelSerializer):
    # The requester's own role, set by the view.
    role = serializers.CharField(read_only=True)

    class Meta:
        model = Organization
        fields = ["id", "name", "time_zone", "role", "created_at"]
        read_only_fields = ["id", "created_at"]


class MembershipSerializer(serializers.ModelSerializer):
    user = UserSerializer(read_only=True)

    class Meta:
        model = Membership
        fields = ["id", "user", "role", "created_at"]
        read_only_fields = ["id", "created_at"]


class InvitationSerializer(serializers.ModelSerializer):
    invited_by = serializers.SlugRelatedField(slug_field="email", read_only=True)

    class Meta:
        model = Invitation
        fields = ["id", "email", "role", "invited_by", "created_at", "expires_at"]
        read_only_fields = ["id", "created_at", "expires_at"]

    def validate_email(self, value):
        return BaseUserManager.normalize_email(value)


class InvitedOrganizationSerializer(serializers.ModelSerializer):
    class Meta:
        model = Organization
        fields = ["id", "name"]


class MyInvitationSerializer(serializers.ModelSerializer):
    organization = InvitedOrganizationSerializer(read_only=True)
    invited_by = serializers.SlugRelatedField(slug_field="email", read_only=True)

    class Meta:
        model = Invitation
        fields = ["id", "organization", "role", "invited_by", "created_at", "expires_at"]
        read_only_fields = fields
