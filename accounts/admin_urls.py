from django.urls import path

from . import views

urlpatterns = [
    path("stats/", views.admin_stats, name="admin-stats"),
    path("workspaces/<int:project_id>/archive/", views.admin_archive_workspace, name="admin-workspace-archive"),
    path("workspaces/<int:project_id>/unarchive/", views.admin_unarchive_workspace, name="admin-workspace-unarchive"),
    path("users/", views.admin_create_user_view, name="admin-user-create"),
    path("users/<int:user_id>/", views.admin_update_user_view, name="admin-user-update"),
]
