import uuid
from datetime import timedelta
from functools import cache
from zoneinfo import ZoneInfo, available_timezones

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models.functions import Lower
from django.utils import timezone

from apps.core.models import TimeStampedModel

INVITATION_LIFETIME = timedelta(days=7)


@cache
def time_zone_names():
    return frozenset(available_timezones())


def validate_time_zone(value):
    if value not in time_zone_names():
        raise ValidationError(f"{value!r} is not a known time zone.", code="invalid")


class Organization(TimeStampedModel):
    """A tenant: one restaurant, bakery, or cloud kitchen business."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=150)
    # The kitchen's IANA time zone, e.g. "Europe/London". It decides what
    # "today" means for expiry dates.
    time_zone = models.CharField(max_length=64, default="UTC", validators=[validate_time_zone])

    class Meta:
        ordering = ["name", "created_at"]

    def __str__(self):
        return self.name

    def local_today(self):
        return timezone.now().astimezone(ZoneInfo(self.time_zone)).date()


class Role(models.TextChoices):
    OWNER = "owner", "Owner"
    MANAGER = "manager", "Manager"
    STAFF = "staff", "Staff"


ROLE_RANK = {Role.STAFF: 1, Role.MANAGER: 2, Role.OWNER: 3}


def role_at_least(role, minimum):
    return ROLE_RANK[role] >= ROLE_RANK[minimum]


class Membership(TimeStampedModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="memberships"
    )
    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name="memberships"
    )
    role = models.CharField(max_length=20, choices=Role.choices, default=Role.STAFF)

    class Meta:
        ordering = ["created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["user", "organization"], name="organizations_membership_unique"
            ),
            models.CheckConstraint(
                condition=models.Q(role__in=Role.values), name="organizations_membership_role_valid"
            ),
        ]

    def __str__(self):
        return f"{self.user} ({self.role}) in {self.organization}"


class InvitationQuerySet(models.QuerySet):
    def pending(self):
        return self.filter(expires_at__gt=timezone.now())

    def for_email(self, email):
        return self.filter(email__iexact=email)


def invitation_expiry():
    return timezone.now() + INVITATION_LIFETIME


class Invitation(TimeStampedModel):
    """
    An offer to join an organization, addressed to an email address.

    Invitations are created the same way whether or not the address has an
    account, so inviting someone never reveals who is registered. The invitee
    accepts or declines after signing in with that address. Accepted, declined,
    and revoked invitations are deleted.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name="invitations"
    )
    email = models.EmailField()
    role = models.CharField(max_length=20, choices=Role.choices, default=Role.STAFF)
    invited_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+"
    )
    expires_at = models.DateTimeField(default=invitation_expiry)

    objects = InvitationQuerySet.as_manager()

    class Meta:
        ordering = ["created_at"]
        constraints = [
            models.UniqueConstraint(
                "organization", Lower("email"), name="organizations_invitation_unique_email"
            ),
            models.CheckConstraint(
                condition=models.Q(role__in=Role.values), name="organizations_invitation_role_valid"
            ),
        ]

    def __str__(self):
        return f"{self.email} ({self.role}) to {self.organization}"
