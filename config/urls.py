"""Root URL configuration for SimpleAudit Studio."""
import os as _os

from django.conf import settings
from django.contrib import admin
from django.http import Http404
from django.shortcuts import redirect
from django.urls import include, path
from django.views.generic import RedirectView
from django.views.static import serve as _static_serve
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView

from accounts.views import healthz, readyz
from infra.health_api import health_panel_api
from infra.seo import LandingView, llms_txt, robots_txt, sitemap_xml
from infra.ui import (
    AdminView,
    CompareView,
    ConnectionDeleteView,
    DashboardExportView,
    DashboardView,
    DiscoverModelsView,
    ExperimentDetailView,
    ExperimentsView,
    HealthView,
    IndexView,
    LoginView,
    ModelsView,
    MonitorActionView,
    MonitorDetailView,
    MonitorsView,
    NewExperimentView,
    ProfileView,
    RegisterView,
    RunArchiveView,
    RunCancelView,
    RunDetailView,
    RunExportView,
    RunRenameView,
    RunResultsFragmentView,
    RunResultView,
    ScenarioCreateView,
    ScenarioDeleteView,
    ScenarioDiffView,
    ScenarioEditView,
    ScenarioExportView,
    ScenarioImportView,
    ScenarioRevertView,
    ScenarioSetCreateView,
    ScenarioSetDeleteView,
    ScenarioSetRenameView,
    ScenariosView,
    WorkOSCallbackView,
    WorkOSLoginView,
    WorkOSVerifyView,
    WorkspacesView,
    auto_login_view,
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
_landing_view = LandingView.as_view()
_dashboard_view = DashboardView.as_view()


def home_view(request, *args, **kwargs):
    """Signed in: the dashboard. Signed out: the public landing page."""
    if request.user.is_authenticated:
        return _dashboard_view(request, *args, **kwargs)
    return _landing_view(request, *args, **kwargs)


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
    path("api/admin/", include("accounts.admin_urls")),
    path("api/", include("scenarios.urls")),
    path("api/", include("model_registry.urls")),
    path("api/", include("audits.urls")),
    # Public landing page (indexable, no auth) — the site's SEO surface
    # "/" is the dashboard when signed in and the public landing page otherwise.
    path("", home_view, name="dashboard"),
    path("landing/", LandingView.as_view(always_show=True), name="landing_page"),
    # UI (server-rendered CBVs)
    path("index", IndexView.as_view(), name="index"),
    path("login/", LoginView.as_view(), name="login"),
    # Local one-liner demo only (404 unless MINIMAL_CONFIG): the CLI opens this
    # in the default browser to land the user signed-in on the dashboard.
    path("auto-login/", auto_login_view, name="auto_login"),
    path("register/", RegisterView.as_view(), name="register"),
    path("logout/", logout_view, name="logout"),
    path("auth/workos/login/", WorkOSLoginView.as_view(), name="workos_login"),
    path("auth/workos/verify/", WorkOSVerifyView.as_view(), name="workos_verify"),
    path("auth/workos/callback/", WorkOSCallbackView.as_view(), name="workos_callback"),
    # Old dashboard URL: kept only as a redirect for bookmarks (query preserved).
    path("dashboard/", RedirectView.as_view(url="/", query_string=True, permanent=True)),
    path("workspaces/", WorkspacesView.as_view(), name="workspaces"),
    path("admin-settings/", AdminView.as_view(), name="admin_settings"),
    path("profile/", ProfileView.as_view(), name="profile"),
    path("health/", HealthView.as_view(), name="health"),
    path("experiments/new/", NewExperimentView.as_view(), name="new_experiment"),
    path("experiments/", ExperimentsView.as_view(), name="experiments"),
    path("experiments/<int:experiment_id>/", ExperimentDetailView.as_view(), name="experiment_detail"),
    path("monitors/", MonitorsView.as_view(), name="monitors"),
    path("monitors/<int:monitor_id>/", MonitorDetailView.as_view(), name="monitor_detail"),
    path(
        "monitors/<int:monitor_id>/<str:action>/",
        MonitorActionView.as_view(),
        name="monitor_action",
    ),
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
    path("models/connection-delete/<int:conn_id>/", ConnectionDeleteView.as_view(), name="connection_delete"),
    path("compare/", CompareView.as_view(), name="compare"),
    path("runs/<int:run_id>/", RunDetailView.as_view(), name="run_detail"),
    path("runs/<int:run_id>/cancel/", RunCancelView.as_view(), name="run_cancel"),
    path("runs/<int:run_id>/archive/", RunArchiveView.as_view(), name="run_archive"),
    path("runs/<int:run_id>/rename/", RunRenameView.as_view(), name="run_rename"),
    path("runs/<int:run_id>/results/<int:result_id>/", RunResultView.as_view(), name="run_result"),
    path("runs/<int:run_id>/export/", RunExportView.as_view(), name="run_export"),
    path("runs/<int:run_id>/results-fragment/", RunResultsFragmentView.as_view(), name="run_results_fragment"),
    path("export.csv", DashboardExportView.as_view(), name="dashboard_export"),
]
