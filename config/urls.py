"""Root URL configuration for SimpleAudit Studio."""
import os as _os

from django.conf import settings
from django.contrib import admin
from django.http import Http404
from django.shortcuts import redirect
from django.urls import include, path
from django.views.static import serve as _static_serve
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView

from accounts.views import healthz, readyz
from infra.health_api import health_panel_api
from infra.seo import LandingView, llms_txt, robots_txt, sitemap_xml
from infra.ui import (
    AuditArchiveView,
    AuditCancelView,
    AuditDetailView,
    AuditExportView,
    AuditRenameView,
    CompareView,
    ConnectionDeleteView,
    DashboardExportView,
    DashboardView,
    DiscoverModelsView,
    HealthView,
    IndexView,
    LoginView,
    ModelDeleteView,
    ModelsView,
    NewAuditView,
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
    WorkOSCallbackView,
    WorkOSLoginView,
    WorkOSVerifyView,
    WorkspacesView,
    logout_view,
)

# --- Static file serving ---------------------------------------------------
# For the canonical Docker Compose self-hosted deployment Django serves its
# own static files (no reverse proxy needed). collectstatic runs at image
# build time (see Dockerfile). We use django.views.static.serve (not the
# staticfiles app's serve) because the latter refuses to serve when DEBUG=False.
_STATIC_ROOT = str(settings.STATIC_ROOT)


def _serve_static(request, path):
    """Serve from STATIC_ROOT, then source dirs, then finders.

    Order matters for resilience against stale Docker layer caches (a known
    HF Spaces issue): collectstatic runs at build time, so a freshly added
    asset can be missing from STATIC_ROOT even after a "rebuild" if the
    collectstatic layer was cached. Falling back to the source static dirs
    (which COPY . . always brings up to date) means new assets serve as soon
    as the app code layer is fresh, independent of collectstatic caching.
    """
    full_path = _os.path.join(_STATIC_ROOT, path)
    if _os.path.isfile(full_path):
        return _static_serve(request, path, document_root=_STATIC_ROOT)

    # Fall back to the configured source static dirs (e.g. <repo>/static).
    for source_dir in getattr(settings, "STATICFILES_DIRS", []):
        candidate = _os.path.join(str(source_dir), path)
        if _os.path.isfile(candidate):
            return _static_serve(request, path, document_root=str(source_dir))

    from django.contrib.staticfiles.finders import find

    found = find(path)
    if found:
        import posixpath

        return _static_serve(request, path, document_root=posixpath.dirname(found))
    raise Http404(f"Static file not found: {path}")


def _favicon(request):
    """Serve the favicon at /favicon.ico (browsers auto-request this path).

    The real icon is referenced via <link> in base.html, but browsers also probe
    /favicon.ico by default. We redirect to the SVG favicon so the tab icon loads
    even for clients that ignore <link> tags, avoiding a 404 in logs.
    """
    return redirect("static-serve", path="favicon.svg")


# --- URL patterns ----------------------------------------------------------
urlpatterns = [
    path("static/<path:path>", _serve_static, name="static-serve"),
    path("favicon.ico", _favicon, name="favicon"),
    path("admin/", admin.site.urls),
    path("healthz", healthz, name="healthz"),
    path("readyz", readyz, name="readyz"),
    # Technical SEO (public, no auth)
    path("sitemap.xml", sitemap_xml, name="sitemap"),
    path("robots.txt", robots_txt, name="robots"),
    path("llms.txt", llms_txt, name="llms"),
    # API
    path("api/health/", health_panel_api, name="health_panel_api"),
    path("api/schema/", SpectacularAPIView.as_view(), name="schema"),
    path("api/docs/", SpectacularSwaggerView.as_view(url_name="schema"), name="swagger-ui"),
    path("api/auth/", include("accounts.auth_urls")),
    path("api/projects/", include("accounts.project_urls")),
    path("api/", include("scenarios.urls")),
    path("api/", include("model_registry.urls")),
    path("api/", include("audits.urls")),
    # Public landing page (indexable, no auth) — the site's SEO surface
    path("", LandingView.as_view(), name="landing"),
    # UI (server-rendered CBVs)
    path("index", IndexView.as_view(), name="index"),
    path("login/", LoginView.as_view(), name="login"),
    path("register/", RegisterView.as_view(), name="register"),
    path("logout/", logout_view, name="logout"),
    path("auth/workos/login/", WorkOSLoginView.as_view(), name="workos_login"),
    path("auth/workos/verify/", WorkOSVerifyView.as_view(), name="workos_verify"),
    path("auth/workos/callback/", WorkOSCallbackView.as_view(), name="workos_callback"),
    path("dashboard/", DashboardView.as_view(), name="dashboard"),
    path("workspaces/", WorkspacesView.as_view(), name="workspaces"),
    path("health/", HealthView.as_view(), name="health"),
    path("audits/new/", NewAuditView.as_view(), name="new_audit"),
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
    path("models/discover/", DiscoverModelsView.as_view(), name="models_discover"),
    path("models/delete/<int:endpoint_id>/", ModelDeleteView.as_view(), name="model_delete"),
    path("models/connection-delete/<int:conn_id>/", ConnectionDeleteView.as_view(), name="connection_delete"),
    path("compare/", CompareView.as_view(), name="compare"),
    path("audits/<int:run_id>/", AuditDetailView.as_view(), name="audit_detail"),
    path("audits/<int:run_id>/cancel/", AuditCancelView.as_view(), name="audit_cancel"),
    path("audits/<int:run_id>/archive/", AuditArchiveView.as_view(), name="audit_archive"),
    path("audits/<int:run_id>/rename/", AuditRenameView.as_view(), name="audit_rename"),
    path("audits/<int:run_id>/results/<int:result_id>/", ScenarioResultDetailView.as_view(), name="scenario_result_detail"),
    path("audits/<int:run_id>/export/", AuditExportView.as_view(), name="audit_export"),
    path("dashboard/export.csv", DashboardExportView.as_view(), name="dashboard_export"),
]
