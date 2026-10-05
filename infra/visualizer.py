"""Result visualizer — the SimpleAudit single-file HTML viewer, absorbed into Studio.

This module ports the logic that used to live in the ``simpleaudit`` core's
FastAPI ``visualization/server.py`` (``simpleaudit serve`` / ``export-html``)
into thin Django views, and serves the two self-contained HTML assets
(``static/visualizer.html`` and ``static/scenario_viewer.html``) that carry all
the rendering, PDF export, image lightbox, and file-tree browsing.

Two ways to get results in front of the viewer:

* **Server-side** (primary): start Studio with ``--results_dir <path>`` and the
  file-tree endpoints read that local directory of JSON results. This is the
  client use case — point the server at a folder of dumped ``simpleaudit``
  results and browse them.
* **Client-side** (bonus): the drag-drop viewer page (``/visualizer/upload/``)
  loads ``scenario_viewer.html``, which reads files straight from the browser
  via the File System Access API — no server round-trip.

The standalone HTML export (``/runs/<id>/export-html/``) inlines a run's JSON
into the visualizer template so the output opens in any browser with no server.
"""
import json
import logging
import os
import time

from django.conf import settings
from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404
from django.views.generic import View

from infra.ui import ProjectMixin

logger = logging.getLogger(__name__)

# Simple in-memory cache for file tree (TTL: 60 seconds)
_file_tree_cache = {
    "data": None,
    "timestamp": 0,
    "dir": None,
}
_CACHE_TTL = 60  # seconds


# --- results directory (set by the CLI from --results_dir) ------------------
# Read at request time so the CLI can set it after import and tests can patch
# it per-test without touching a module global at import.
def results_dir() -> str | None:
    """The configured results directory, or None when not set."""
    return getattr(settings, "VISUALIZER_RESULTS_DIR", None)


def set_results_dir(path: str | None) -> None:
    """Set the results directory for the file-tree endpoints (CLI/tests)."""
    settings.VISUALIZER_RESULTS_DIR = path


# --- audit-data shape detection (ported from simpleaudit core) ---------------
def _looks_like_audit_result(obj: object) -> bool:
    return (
        isinstance(obj, dict)
        and ("scenario_name" in obj or "name" in obj)
        and "severity" in obj
    )


def _experiment_models(data: object) -> list[str]:
    """Model labels in an experiment file that have at least one loadable run.

    A run is loadable when it is a dict holding a non-empty list of
    audit-shaped results. Both the file tree and the JSON endpoint derive their
    notion of "experiment" from this list, so the tree never shows an entry the
    endpoint would refuse to serve.
    """
    if not isinstance(data, dict):
        return []
    runs = data.get("runs")
    if not isinstance(runs, dict):
        return []
    models = []
    for label, run_list in runs.items():
        entries = run_list if isinstance(run_list, list) else [run_list]
        for entry in entries:
            if (
                isinstance(entry, dict)
                and isinstance(entry.get("results"), list)
                and entry["results"]
                and all(_looks_like_audit_result(item) for item in entry["results"])
            ):
                models.append(label)
                break
    return models


def is_valid_audit_data(data) -> bool:
    """Whether parsed JSON has the shape of audit results.

    Accepts the three shapes the visualizer renders: a list of results, a
    ``{"results": [...]}`` object, or a multi-model experiment
    ``{"runs": {"model": [{"results": [...]}]}}``.
    """
    if isinstance(data, list):
        return bool(data) and all(_looks_like_audit_result(item) for item in data)
    if isinstance(data, dict) and "results" in data:
        results = data["results"]
        return (
            isinstance(results, list)
            and bool(results)
            and all(_looks_like_audit_result(item) for item in results)
        )
    if isinstance(data, dict) and "runs" in data:
        return bool(_experiment_models(data))
    return False


def get_file_tree(directory: str, base_path: str = "") -> list[dict]:
    """Recursively build the JSON file tree for the visualizer.

    Folders are included only when they contain at least one loadable JSON
    file (directly or in a subdirectory); experiment files are tagged with
    their model labels so the UI can render a model picker.

    Each JSON file is parsed here (not just name-checked) because that is what
    lets us drop non-audit files and classify experiments. The view wraps this
    in a short-lived cache, so the tree is only rebuilt on a cache miss.
    """
    items = []
    try:
        entries = sorted(os.listdir(directory))
    except (PermissionError, OSError):
        return items

    for entry in entries:
        full_path = os.path.join(directory, entry)
        rel_path = os.path.join(base_path, entry) if base_path else entry

        if os.path.isdir(full_path):
            children = get_file_tree(full_path, rel_path)
            if children:
                items.append(
                    {"name": entry, "type": "folder", "path": rel_path, "children": children}
                )
        elif os.path.isfile(full_path) and entry.endswith(".json"):
            try:
                with open(full_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except (OSError, json.JSONDecodeError):
                logger.debug("Skipping unreadable/invalid JSON in results dir: %s", full_path)
                continue
            experiment_models = _experiment_models(data)
            if experiment_models:
                items.append(
                    {
                        "name": entry,
                        "type": "experiment",
                        "path": rel_path,
                        "models": experiment_models,
                    }
                )
            elif is_valid_audit_data(data):
                items.append({"name": entry, "type": "file", "path": rel_path})

    return items


def _resolve_results_path(file_path: str) -> str | None:
    """Resolve a relative path inside the results dir, or None if it escapes it.

    Guards against path traversal and symlink escapes the same way the core
    server did: the resolved real path must stay under the results root.
    """
    root_dir = results_dir()
    if not root_dir:
        return None
    root = os.path.realpath(os.path.abspath(root_dir))
    try:
        full_path = os.path.realpath(os.path.join(root, file_path))
    except (ValueError, TypeError):
        return None
    if not full_path.startswith(root + os.sep):
        return None
    return full_path


def _read_asset(name: str) -> str:
    """Read a static asset by name, from STATIC_ROOT, source dirs, or finders."""
    static_root = str(settings.STATIC_ROOT)
    for root in [static_root, *getattr(settings, "STATICFILES_DIRS", [])]:
        candidate = os.path.join(str(root), name)
        if os.path.isfile(candidate):
            with open(candidate, "r", encoding="utf-8") as f:
                return f.read()
    from django.contrib.staticfiles.finders import find

    found = find(name)
    if found:
        with open(found, "r", encoding="utf-8") as f:
            return f.read()
    raise Http404(f"Static asset not found: {name}")


def _inject_footer(html: str) -> str:
    """Replace the ``<!-- VISUALIZER_FOOTER -->`` placeholder with the Studio footer.

    The visualizer pages are standalone static assets (not Django templates), so
    they can't ``{% include %}`` the shared footer. We render ``partials/footer.html``
    here and splice it in at serve time, keeping the footer in one place so the
    visualizer stays in sync with the rest of the Studio.
    """
    if "<!-- VISUALIZER_FOOTER -->" not in html:
        return html
    from django.template.loader import render_to_string

    footer = render_to_string("partials/footer.html", {"footer_class": "py-2 px-2"})
    return html.replace("<!-- VISUALIZER_FOOTER -->", footer)


def _sidebar_assets() -> tuple[str, str]:
    """Return ``(css, js)`` for the Studio app-shell sidebar, extracted from
    ``templates/base.html`` so the visualizer stays in sync with the Studio.

    The CSS is the ``<style>`` block that styles ``#sidebar``; the JS is the IIFE
    that powers the collapse-to-rail / mobile-drawer behaviour.
    """
    from pathlib import Path

    base = (Path(__file__).resolve().parent.parent / "templates" / "base.html").read_text(encoding="utf-8")

    # CSS: the <style> block that begins with the app-shell sidebar rules.
    css_start = base.index("/* ── App shell sidebar")
    css_end = base.index("</style>", css_start)
    css = base[css_start:css_end]

    # JS: the sidebar-collapse IIFE (in its own <script> block, anchored by a
    # unique comment). Strip the <script> tags so the caller can wrap it.
    anchor = "// Sidebar: collapse to an icon rail"
    i = base.index(anchor)
    js_start = base.rindex("<script>", 0, i) + len("<script>")
    js_end = base.index("</script>", i)
    js = base[js_start:js_end]
    return css, js


def _inject_sidebar(html: str, request) -> str:
    """Replace the ``<!-- VISUALIZER_SIDEBAR -->`` / ``_CSS`` / ``_JS`` placeholders
    with the Studio app-shell sidebar so the visualizer reads as part of the Studio.

    The sidebar is ``partials/visualizer_sidebar.html`` — the same frame as the
    Studio sidebar (logo header + user footer, inherited markup) but with the JSON
    file tree as its body instead of the nav menu. It's rendered with the request
    in context so the ``gravatar_url`` / ``auth`` context processors populate the
    user footer. The collapse/drawer CSS + JS are extracted from ``base.html``.
    """
    if "<!-- VISUALIZER_SIDEBAR -->" not in html:
        return html
    from django.template.loader import render_to_string

    # render_to_string with a plain context does not run context processors, so
    # pass the values the partial needs explicitly (user, gravatar).
    user = getattr(request, "user", None)
    gravatar = None
    if user is not None and user.is_authenticated and user.email:
        import hashlib

        gravatar = (
            "https://www.gravatar.com/avatar/"
            + hashlib.md5(user.email.strip().lower().encode("utf-8")).hexdigest()
            + "?d=identicon&s=128"
        )
    sidebar = render_to_string(
        "partials/visualizer_sidebar.html",
        {"request": request, "user": user, "gravatar_url": gravatar},
    )
    css, js = _sidebar_assets()

    html = html.replace("<!-- VISUALIZER_SIDEBAR_CSS -->", f"<style>\n{css}\n</style>")
    html = html.replace("<!-- VISUALIZER_SIDEBAR -->", sidebar)
    html = html.replace("<!-- VISUALIZER_SIDEBAR_JS -->", f"<script>\n{js}\n</script>")
    return html


def build_standalone_html(data, name: str) -> str:
    """Inline audit data into the visualizer template as a standalone HTML file.

    The output opens directly in a browser (or can be sent to someone) — no
    server and no upload step. The visualizer detects the inlined data on load
    and renders it as a custom upload.
    """
    if not is_valid_audit_data(data):
        raise ValueError("data does not look like SimpleAudit results")

    html = _read_asset("visualizer.html")
    if "</head>" not in html:
        raise ValueError("visualizer.html has no </head> anchor — cannot inline data")

    # Escape so the payload cannot break out of the inline <script> tag.
    payload = json.dumps(data)
    payload = payload.replace("<", "\\u003c").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    name_json = json.dumps(name).replace("<", "\\u003c")
    is_experiment = isinstance(data, dict) and isinstance(data.get("runs"), dict)
    mode_json = json.dumps("experiment" if is_experiment else "single")
    inline = (
        f"<script>window.__inlinedData = {payload}; "
        f"window.__inlinedName = {name_json}; "
        f"window.__standaloneMode = {mode_json};</script>\n"
    )
    html = html.replace("</head>", f"{inline}</head>", 1)
    return _inject_footer(html)


# --- views -------------------------------------------------------------------
class VisualizerView(LoginRequiredMixin, View):
    """Serve the file-tree visualizer SPA (``/visualizer/``).

    The SPA is a static asset, but its API calls must be prefixed with
    ``/api/visualizer`` when embedded in Studio. We inject the base into the
    one ``window.__VISUALIZER_API_BASE`` line (the same inline-into-``</head>``
    technique the core used for standalone export) and serve the result.
    """

    def get(self, request):
        html = _read_asset("visualizer.html")
        html = html.replace(
            "window.__VISUALIZER_API_BASE = '{{ visualizer_api_base|default:\"\" }}';",
            "window.__VISUALIZER_API_BASE = '/api/visualizer';",
        )
        html = _inject_sidebar(html, request)
        html = _inject_footer(html)
        return HttpResponse(html, content_type="text/html; charset=utf-8")


class ScenarioViewerView(LoginRequiredMixin, View):
    """Serve the offline drag-drop viewer (``/visualizer/upload/``).

    ``scenario_viewer.html`` reads files straight from the browser via the File
    System Access API — no server round-trip — so it works fully offline.
    """

    def get(self, request):
        html = _read_asset("scenario_viewer.html")
        html = _inject_sidebar(html, request)
        html = _inject_footer(html)
        return HttpResponse(html, content_type="text/html; charset=utf-8")


class VisualizerFilesView(View):
    """``GET /api/visualizer/files`` → ``{"tree": [...]}`` of the results dir."""

    def get(self, request):
        root_dir = results_dir()
        if not root_dir:
            # The upload visualizer is still available, and the server-side
            # tree should remain a valid empty state when no folder is set.
            return JsonResponse({"tree": [], "configured": False})
        if not os.path.isdir(root_dir):
            try:
                os.makedirs(root_dir, exist_ok=True)
            except OSError:
                return JsonResponse({"error": "Results directory is unavailable"}, status=503)
        
        # Check cache
        now = time.time()
        if (_file_tree_cache["data"] is not None and 
            _file_tree_cache["dir"] == root_dir and
            now - _file_tree_cache["timestamp"] < _CACHE_TTL):
            return JsonResponse({"tree": _file_tree_cache["data"]})
        
        # Cache miss - build the tree
        tree = get_file_tree(root_dir)
        
        # Update cache
        _file_tree_cache["data"] = tree
        _file_tree_cache["timestamp"] = now
        _file_tree_cache["dir"] = root_dir
        
        return JsonResponse({"tree": tree})


class VisualizerJsonView(View):
    """``GET /api/visualizer/json/<path>`` → the JSON content of a result file."""

    def get(self, request, file_path: str):
        full_path = _resolve_results_path(file_path)
        if full_path is None:
            return JsonResponse({"error": "Access denied"}, status=403)
        if not os.path.isfile(full_path):
            return JsonResponse({"error": "File not found"}, status=404)
        if os.path.splitext(full_path)[1].lower() != ".json":
            return JsonResponse({"error": "Not a JSON file"}, status=400)
        try:
            with open(full_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except json.JSONDecodeError as exc:
            return JsonResponse({"error": f"Invalid JSON: {exc}"}, status=400)
        except OSError:
            return JsonResponse({"error": "Error reading file"}, status=500)
        if not is_valid_audit_data(data):
            return JsonResponse({"error": "Not an audit results file"}, status=403)
        return JsonResponse(data)


class VisualizerImageView(View):
    """``GET /api/visualizer/image?uri=...`` → ``{"data_uri": ...}`` for previews.

    Reuses the audit's own ``image_data_uri`` loader so local paths, http(s) and
    s3 resolve the same way they did at audit time, and non-images are rejected
    identically. Returns a data URI (not raw bytes) so the frontend can assign
    it to an ``<img>`` src.
    """

    def get(self, request):
        from simpleaudit.utils import image_data_uri

        uri = request.GET.get("uri", "")
        if not uri:
            return JsonResponse({"error": "Missing uri"}, status=400)
        try:
            data_uri = image_data_uri(uri)
        except ValueError:
            return JsonResponse({"error": "Not a previewable image"}, status=415)
        except FileNotFoundError:
            return JsonResponse({"error": "Image not found"}, status=404)
        except OSError:
            return JsonResponse({"error": "Error reading image"}, status=500)
        return JsonResponse({"data_uri": data_uri})


class VisualizerAuthView(View):
    """``GET /api/visualizer/auth`` → auth status for the visualizer SPA.

    The visualizer.html carries an auth overlay that expects this endpoint.
    Studio already gates the page behind login, so auth is reported as
    disabled (the overlay stays hidden); the endpoint exists so the SPA's
    startup check doesn't 404.
    """

    def get(self, request):
        return JsonResponse({"ok": True, "enabled": False, "contact_email": ""})


class RunExportHtmlView(ProjectMixin, View):
    """``GET /runs/<id>/export-html/`` → standalone single-file HTML export.

    Inlines the run's results JSON into the visualizer template so the output
    opens in any browser with no server. Mirrors the core ``export-html``
    command, but sources the data from the run's stored results.
    """

    def get(self, request, run_id):
        from audits.events import ScenarioResult
        from audits.models import AuditRun
        from scenarios.models import ScenarioSetVersionItem

        run = get_object_or_404(AuditRun, pk=run_id, project=request.project)

        items = {
            str(vi.pk): vi
            for vi in ScenarioSetVersionItem.objects.filter(
                version=run.scenario_set_version
            ).select_related("scenario")
        }
        results = []
        for sr in ScenarioResult.objects.filter(run_id=run.id).order_by("id"):
            item = items.get(sr.version_item_id)
            r = sr.result or {}
            results.append(
                {
                    "scenario_name": item.scenario.title if item else str(sr.version_item_id),
                    "severity": r.get("severity", sr.status),
                    "summary": r.get("summary", ""),
                    "result": r,
                }
            )

        data = {"results": results}
        name = f"{run.name} (run {run.id})"
        try:
            html = build_standalone_html(data, name)
        except ValueError as exc:
            return HttpResponse(str(exc), status=400)

        safe_name = "".join(c if c.isalnum() or c in "-_." else "_" for c in run.name)[:60] or "run"
        response = HttpResponse(html, content_type="text/html; charset=utf-8")
        response["Content-Disposition"] = f'attachment; filename="audit_{safe_name}_{run.id}.html"'
        return response
