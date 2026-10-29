from django.urls import path

from . import views

urlpatterns = [
    path("projects/<int:project_id>/audit-runs/", views.list_audit_runs, name="audit-run-list"),
    path("projects/<int:project_id>/audit-runs/create/", views.create_audit_run_view, name="audit-run-create"),
    path("projects/<int:project_id>/audit-runs/<int:run_id>/", views.get_audit_run, name="audit-run-detail"),
    path(
        "projects/<int:project_id>/audit-runs/<int:run_id>/cancel/",
        views.cancel_audit_run,
        name="audit-run-cancel",
    ),
    path(
        "projects/<int:project_id>/audit-runs/<int:run_id>/results/",
        views.list_audit_run_results,
        name="audit-run-results",
    ),
    path(
        "projects/<int:project_id>/audit-runs/<int:run_id>/export/",
        views.export_audit_run,
        name="audit-run-export",
    ),
    path(
        "projects/<int:project_id>/audit-runs/<int:run_id>/events/poll/",
        views.poll_audit_run_events,
        name="audit-run-events-poll",
    ),
    path(
        "projects/<int:project_id>/audit-runs/<int:run_id>/events/",
        views.stream_audit_run_events,
        name="audit-run-events",
    ),
    path(
        "projects/<int:project_id>/audit-runs/compare/",
        views.compare_audit_runs,
        name="audit-run-compare",
    ),
]
