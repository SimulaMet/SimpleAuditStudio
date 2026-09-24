from django.urls import path

from . import views

urlpatterns = [
    path("", views.list_workspaces, name="workspace-list"),
    path("create/", views.create_workspace_view, name="workspace-create"),
    path("switch/", views.switch_workspace, name="workspace-switch"),
    path("<int:project_id>/", views.workspace_detail, name="workspace-detail"),
    path("<int:project_id>/members/", views.list_members, name="workspace-members"),
    path("<int:project_id>/members/add/", views.add_member, name="workspace-member-add"),
    path(
        "<int:project_id>/members/<int:user_id>/",
        views.update_or_remove_member,
        name="workspace-member-update",
    ),
]
