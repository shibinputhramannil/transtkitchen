from django.urls import path

from . import views

organization = "organizations/<uuid:organization_id>/"

urlpatterns = [
    path("organizations/", views.OrganizationListCreateView.as_view(), name="organization-list"),
    path(organization, views.OrganizationDetailView.as_view(), name="organization-detail"),
    path(f"{organization}members/", views.MembershipListView.as_view(), name="membership-list"),
    path(
        f"{organization}members/<uuid:pk>/",
        views.MembershipDetailView.as_view(),
        name="membership-detail",
    ),
    path(
        f"{organization}invitations/",
        views.OrganizationInvitationListCreateView.as_view(),
        name="organization-invitation-list",
    ),
    path(
        f"{organization}invitations/<uuid:pk>/",
        views.OrganizationInvitationDetailView.as_view(),
        name="organization-invitation-detail",
    ),
    path("invitations/", views.MyInvitationListView.as_view(), name="invitation-list"),
    path(
        "invitations/<uuid:pk>/accept/",
        views.AcceptInvitationView.as_view(),
        name="invitation-accept",
    ),
    path(
        "invitations/<uuid:pk>/decline/",
        views.DeclineInvitationView.as_view(),
        name="invitation-decline",
    ),
]
