from django.urls import path

from . import views

urlpatterns = [
    path("projects/<int:project_id>/model-endpoints/", views.list_model_endpoints, name="model-endpoint-list"),
    path(
        "projects/<int:project_id>/model-endpoints/create/",
        views.create_model_endpoint_view,
        name="model-endpoint-create",
    ),
    path("projects/<int:project_id>/audit-profiles/", views.list_audit_profiles, name="audit-profile-list"),
    path(
        "projects/<int:project_id>/audit-profiles/create/",
        views.create_audit_profile_view,
        name="audit-profile-create",
    ),
    path("models/ping/<int:model_pk>/", views.ping_model, name="model-ping"),
]
