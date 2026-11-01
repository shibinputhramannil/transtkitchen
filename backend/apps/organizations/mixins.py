from functools import cached_property

from django.shortcuts import get_object_or_404
from rest_framework.permissions import IsAuthenticated

from .models import Membership, Role
from .permissions import HasOrganizationRole


class OrganizationScopedMixin:
    """
    Base for every API view under /api/organizations/<organization_id>/.

    The organization is looked up through the requester's own membership, so a
    user outside the organization gets a 404, exactly as if it did not exist.
    Subclasses must build their querysets from self.organization and set it on
    new objects themselves, never from request data.

    minimum_role applies to every method; override get_minimum_role() for
    per-method rules.
    """

    permission_classes = [IsAuthenticated, HasOrganizationRole]
    minimum_role = Role.STAFF

    def get_minimum_role(self):
        return self.minimum_role

    @cached_property
    def membership(self):
        return get_object_or_404(
            Membership.objects.select_related("organization"),
            user=self.request.user,
            organization_id=self.kwargs["organization_id"],
        )

    @property
    def organization(self):
        return self.membership.organization
