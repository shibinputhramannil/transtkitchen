from rest_framework.permissions import BasePermission

from .models import Role, role_at_least


def can_manage(actor_role, target_role):
    """Owners manage everyone; managers manage staff only."""
    return actor_role == Role.OWNER or (actor_role == Role.MANAGER and target_role == Role.STAFF)


class HasOrganizationRole(BasePermission):
    """
    Allows the request when the requester's role in the view's organization is
    at least view.get_minimum_role(). Non-members never pass: resolving
    view.membership raises a 404 first.
    """

    message = "Your role in this organization does not allow this action."

    def has_permission(self, request, view):
        return role_at_least(view.membership.role, view.get_minimum_role())
