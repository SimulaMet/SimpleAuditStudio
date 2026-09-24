from django.urls import path

from . import views

urlpatterns = [
    path("projects/<int:project_id>/model-endpoints/", views.list_model_endpoints, name="model-endpoint-list"),
    path(
        "projects/<int:project_id>/model-endpoints/create/",
        views.create_model_endpoint_view,
        name="model-endpoint-create",
    ),
    path("models/ping-connection/<int:conn_pk>/", views.ping_connection, name="conn-ping"),
]
