"""Root URL configuration for SimpleAudit Studio."""
import os as _os

from django.conf import settings
from django.contrib import admin
from django.http import Http404
from django.urls import include, path
from django.views.static import serve as _static_serve
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView

from infra.ui import (
    AuditCancelView,
    AuditDetailView,
    AuditExportView,
    CompareView,
    DashboardExportView,
    DashboardView,
    HealthView,
    IndexView,
    LoginView,
    ModelDeleteView,
    ModelsView,
    NewAuditView,
    ProfileDeleteView,
    QueueView,
    RegisterView,
    ScenarioCreateView,
    ScenarioDeleteView,
    ScenarioDiffView,
    ScenarioEditView,
    ScenarioExportView,
    ScenarioImportView,
    ScenarioResultDetailView,
    ScenarioRevertView,
    ScenarioSetCreateView,
    ScenarioSetDeleteView,
    ScenarioSetRenameView,
    ScenariosView,
    logout_view,
)
from accounts.views import healthz, readyz
from infra.health_api import health_panel_api

# --- Static file serving ---------------------------------------------------
# For the canonical Docker Compose self-hosted deployment Django serves its
# own static files (no reverse proxy needed). collectstatic runs at image
# build time (see Dockerfile). We use django.views.static.serve (not the
# staticfiles app's serve) because the latter refuses to serve when DEBUG=False.
_STATIC_ROOT = str(settings.STATIC_ROOT)


def _serve_static(request, path):
    """Serve from STATIC_ROOT if present, else fall back to finders (dev)."""
    full_path = _os.path.join(_STATIC_ROOT, path)
    if _os.path.isfile(full_path):
        return _static_serve(request, path, document_root=_STATIC_ROOT)
    from django.contrib.staticfiles.finders import find

    found = find(path)
    if found:
        import posixpath

        return _static_serve(request, path, document_root=posixpath.dirname(found))
    raise Http404(f"Static file not found: {path}")


# --- URL patterns ----------------------------------------------------------
urlpatterns = [
    path("static/<path:path>", _serve_static, name="static-serve"),
    path("admin/", admin.site.urls),
    path("healthz", healthz, name="healthz"),
    path("readyz", readyz, name="readyz"),
    # API
    path("api/health/", health_panel_api, name="health_panel_api"),
    path("api/schema/", SpectacularAPIView.as_view(), name="schema"),
    path("api/docs/", SpectacularSwaggerView.as_view(url_name="schema"), name="swagger-ui"),
    path("api/auth/", include("accounts.auth_urls")),
    path("api/projects/", include("accounts.project_urls")),
    path("api/", include("scenarios.urls")),
    path("api/", include("model_registry.urls")),
    path("api/", include("audits.urls")),
    # UI (server-rendered CBVs)
    path("", IndexView.as_view(), name="index"),
    path("login/", LoginView.as_view(), name="login"),
    path("register/", RegisterView.as_view(), name="register"),
    path("logout/", logout_view, name="logout"),
    path("dashboard/", DashboardView.as_view(), name="dashboard"),
    path("health/", HealthView.as_view(), name="health"),
    path("audits/new/", NewAuditView.as_view(), name="new_audit"),
    path("queue/", QueueView.as_view(), name="queue"),
    path("scenarios/", ScenariosView.as_view(), name="scenarios"),
    path("scenarios/set-create/", ScenarioSetCreateView.as_view(), name="scenario_set_create"),
    path("scenarios/set-rename/<int:set_id>/", ScenarioSetRenameView.as_view(), name="scenario_set_rename"),
    path("scenarios/set-delete/<int:set_id>/", ScenarioSetDeleteView.as_view(), name="scenario_set_delete"),
    path("scenarios/create/", ScenarioCreateView.as_view(), name="scenario_create"),
    path("scenarios/edit/<int:scenario_id>/", ScenarioEditView.as_view(), name="scenario_edit"),
    path("scenarios/delete/<int:scenario_id>/", ScenarioDeleteView.as_view(), name="scenario_delete"),
    path("scenarios/revert/<int:set_id>/", ScenarioRevertView.as_view(), name="scenario_revert"),
    path("scenarios/diff/<int:set_id>/", ScenarioDiffView.as_view(), name="scenario_diff"),
    path("scenarios/<int:set_id>/export/", ScenarioExportView.as_view(), name="scenario_export"),
    path("scenarios/<int:set_id>/import/", ScenarioImportView.as_view(), name="scenario_import"),
    path("models/", ModelsView.as_view(), name="models"),
    path("models/delete/<int:endpoint_id>/", ModelDeleteView.as_view(), name="model_delete"),
    path("models/profile-delete/<int:profile_id>/", ProfileDeleteView.as_view(), name="profile_delete"),
    path("compare/", CompareView.as_view(), name="compare"),
    path("audits/<int:run_id>/", AuditDetailView.as_view(), name="audit_detail"),
    path("audits/<int:run_id>/cancel/", AuditCancelView.as_view(), name="audit_cancel"),
    path("audits/<int:run_id>/results/<int:result_id>/", ScenarioResultDetailView.as_view(), name="scenario_result_detail"),
    path("audits/<int:run_id>/export/", AuditExportView.as_view(), name="audit_export"),
    path("dashboard/export.csv", DashboardExportView.as_view(), name="dashboard_export"),
]
