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
import threading
import time

from django.conf import settings
from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404
from django.views.generic import View

from infra.ui import ProjectMixin

logger = logging.getLogger(__name__)

# Persistent metadata index: avoids re-parsing unchanged JSON files between
# scans. Keyed by (realpath, mtime_ns, size) → classification result.
# In-memory for the process lifetime; safe to lose (just re-parses).
_metadata_index: dict[tuple[str, int, int], dict] = {}
_METADATA_INDEX_MAX = 50_000

# Optional watchfiles-based file monitor: invalidates the tree cache when
# files in the results dir change. Started lazily on first scan; stopped on
# shutdown. Uses a daemon thread so it never blocks the request.
_watcher_thread: threading.Thread | None = None
_watcher_stop_event: threading.Event | None = None
_watcher_root: str | None = None

# Simple in-memory cache for file tree (TTL: 60 seconds)
_file_tree_cache = {
    "data": None,
    "timestamp": 0,
    "key": None,
}
_CACHE_TTL = 60  # seconds

# Bounded-scan limits (overridable via env vars).
_MAX_INSPECTED_FILES = 5_000
_SCAN_TIME_BUDGET_S = 5.0
_MAX_TREE_DEPTH = 8
_MAX_FILE_SIZE_BYTES = 100 * 1024 * 1024  # 100 MB


def _int_setting(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, "") or default)
    except ValueError:
        return default
    return value if value > 0 else default


def _float_setting(name: str, default: float) -> float:
    try:
        value = float(os.environ.get(name, "") or default)
    except ValueError:
        return default
    return value if value > 0 else default


def _max_inspected_files() -> int:
    return _int_setting("VISUALIZER_MAX_INSPECTED_FILES", _MAX_INSPECTED_FILES)


def _scan_time_budget() -> float:
    return _float_setting("VISUALIZER_SCAN_TIME_BUDGET_S", _SCAN_TIME_BUDGET_S)


def _max_tree_depth() -> int:
    return _int_setting("VISUALIZER_MAX_TREE_DEPTH", _MAX_TREE_DEPTH)


def _max_file_size() -> int:
    return _int_setting("VISUALIZER_MAX_FILE_SIZE_MB", 100) * 1024 * 1024


def _watcher_loop(root: str, stop_event: threading.Event) -> None:
    """Background loop: watch the results dir and invalidate the tree cache."""
    try:
        from watchfiles import watch

        for _changes in watch(root, stop_event=stop_event, yield_on_timeout=True, poll_interval_ms=2000):
            if stop_event.is_set():
                break
            # Any change invalidates the cached tree.
            _file_tree_cache["data"] = None
            _file_tree_cache["key"] = None
    except ImportError:
        logger.debug("watchfiles not available; cache invalidation falls back to TTL only.")
    except Exception:
        logger.debug("watchfiles monitor stopped", exc_info=True)


def _ensure_watcher(root: str) -> None:
    """Start the file watcher for the results dir (idempotent)."""
    global _watcher_thread, _watcher_stop_event, _watcher_root
    if _watcher_thread is not None and _watcher_root == root:
        return
    # Stop any existing watcher for a different root.
    if _watcher_stop_event is not None:
        _watcher_stop_event.set()
    _watcher_stop_event = threading.Event()
    _watcher_root = root
    _watcher_thread = threading.Thread(
        target=_watcher_loop, args=(root, _watcher_stop_event), daemon=True
    )
    _watcher_thread.start()


def _stop_watcher() -> None:
    """Stop the file watcher (called on shutdown)."""
    global _watcher_thread, _watcher_stop_event, _watcher_root
    if _watcher_stop_event is not None:
        _watcher_stop_event.set()
    _watcher_thread = None
    _watcher_stop_event = None
    _watcher_root = None

# Directories that never hold audit results; skipping them keeps the tree walk
# bounded when a user points --results_dir at a broad tree (a project checkout,
# the home dir, ...). Everything else is still walked, so results kept inside
# e.g. ``.raw/`` or ``.simpleaudit/`` subfolders are still found.
_PRUNED_DIRS = frozenset(
    {
        ".git",
        "node_modules",
        "__pycache__",
        ".venv",
        "venv",
        ".tox",
        ".mypy_cache",
        ".ruff_cache",
        ".pytest_cache",
        ".idea",
        ".vscode",
        "dist",
        "build",
        ".cache",
        ".npm",
        ".cargo",
        ".rustup",
        ".gradle",
        ".cocoapods",
        ".yarn",
        ".parcel-cache",
        ".next",
        ".nuxt",
        ".serverless",
        ".terraform",
        ".docker",
        ".codex",
        ".local",
        ".bundle",
        "site-packages",
    }
)


def tree_warnings(results_root: str | None) -> list[str]:
    """Heuristic warnings for the files endpoint (helps find misconfigured dirs)."""
    if not results_root:
        return []
    warnings = []
    root = os.path.realpath(os.path.abspath(results_root))
    if os.path.isdir(os.path.expanduser("~")) and root == os.path.realpath(os.path.expanduser("~")):
        warnings.append(
            "The results directory is your home folder — point --results_dir at a "
            "folder that contains SimpleAudit result JSON files."
        )
    if root == os.path.realpath(os.getcwd()):
        warnings.append(
            "The results directory is the current working directory — point "
            "--results_dir at a folder that contains SimpleAudit result JSON files."
        )
    return warnings


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


def _classify_json_file(full_path: str) -> dict | None:
    """Classify a single JSON file: experiment, file, or None (not audit data).

    Uses the persistent metadata index to skip re-parsing unchanged files.
    Returns a dict with 'type' and optionally 'models', or None.
    """
    try:
        st = os.stat(full_path)
    except OSError:
        return None
    key = (os.path.realpath(full_path), st.st_mtime_ns, st.st_size)
    cached = _metadata_index.get(key)
    if cached is not None:
        return cached

    # Oversized files are skipped (they are almost never single audit results).
    if st.st_size > _max_file_size():
        logger.debug("Skipping oversized JSON (%d bytes): %s", st.st_size, full_path)
        _store_metadata(key, None)
        return None

    try:
        with open(full_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        _store_metadata(key, None)
        return None

    experiment_models = _experiment_models(data)
    if experiment_models:
        result = {"type": "experiment", "models": experiment_models}
    elif is_valid_audit_data(data):
        result = {"type": "file"}
    else:
        result = None
    _store_metadata(key, result)
    return result


def _store_metadata(key: tuple, value: dict | None) -> None:
    """Store a classification result in the metadata index (bounded)."""
    if len(_metadata_index) >= _METADATA_INDEX_MAX:
        # Evict oldest entries (dict preserves insertion order).
        for _ in range(_METADATA_INDEX_MAX // 10):
            _metadata_index.pop(next(iter(_metadata_index)), None)
    _metadata_index[key] = value


def _scan_results_dir(
    root_dir: str,
    max_depth: int,
    max_files: int,
    time_budget: float,
) -> tuple[list[dict], dict]:
    """Bounded, incremental scan of the results directory.

    Returns ``(tree, meta)`` where ``meta`` contains:
    - ``truncated``: bool — True if any limit was hit
    - ``reason``: str | None — which limit was hit (first one)
    - ``inspected``: int — number of JSON files inspected
    - ``elapsed_seconds``: float — wall-clock time of the scan
    """
    deadline = time.monotonic() + time_budget
    inspected = 0
    reason: str | None = None

    def _time_up() -> bool:
        nonlocal reason
        if time.monotonic() >= deadline:
            if reason is None:
                reason = "time_budget"
            return True
        return False

    def _files_cap_hit() -> bool:
        nonlocal reason
        if inspected >= max_files:
            if reason is None:
                reason = "file_limit"
            return True
        return False

    def _depth_hit() -> bool:
        nonlocal reason
        if reason is None:
            reason = "depth_limit"
        return True

    def _walk(directory: str, base_path: str, depth: int) -> list[dict]:
        nonlocal inspected
        items: list[dict] = []
        try:
            entries = sorted(os.listdir(directory))
        except (PermissionError, OSError):
            return items

        for entry in entries:
            if _time_up() or _files_cap_hit():
                break
            full_path = os.path.join(directory, entry)
            rel_path = os.path.join(base_path, entry) if base_path else entry

            if os.path.islink(full_path):
                # Skip symlinks entirely to avoid cycles and escapes.
                continue
            if os.path.isdir(full_path):
                if entry in _PRUNED_DIRS:
                    continue
                if depth >= max_depth:
                    _depth_hit()
                    break
                children = _walk(full_path, rel_path, depth + 1)
                if children:
                    items.append(
                        {"name": entry, "type": "folder", "path": rel_path, "children": children}
                    )
            elif os.path.isfile(full_path) and entry.endswith(".json"):
                inspected += 1
                result = _classify_json_file(full_path)
                if result:
                    item = {"name": entry, "type": result["type"], "path": rel_path}
                    if "models" in result:
                        item["models"] = result["models"]
                    items.append(item)

        return items

    start = time.monotonic()
    tree = _walk(root_dir, "", 0)
    elapsed = time.monotonic() - start

    meta = {
        "truncated": reason is not None,
        "reason": reason,
        "inspected": inspected,
        "elapsed_seconds": round(elapsed, 3),
    }
    return tree, meta


def get_file_tree(directory: str, base_path: str = "") -> list[dict]:
    """Build the JSON file tree for the visualizer (bounded scan).

    This is a convenience wrapper around ``_scan_results_dir`` that applies
    the default limits. Prefer calling ``_scan_results_dir`` directly when you
    need the metadata (truncated/reason/inspected/elapsed_seconds).
    """
    tree, _meta = _scan_results_dir(
        directory,
        max_depth=_max_tree_depth(),
        max_files=_max_inspected_files(),
        time_budget=_scan_time_budget(),
    )
    return tree


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
    """``GET /api/visualizer/api/files/`` → bounded file tree of the results dir.

    Response shape:
    {
        "tree": [...],
        "warnings": [...],
        "truncated": bool,
        "reason": str | null,
        "inspected": int,
        "elapsed_seconds": float,
        "configured": true
    }
    """

    def get(self, request):
        root_dir = results_dir()
        if not root_dir:
            return JsonResponse({"tree": [], "configured": False})
        if not os.path.isdir(root_dir):
            try:
                os.makedirs(root_dir, exist_ok=True)
            except OSError:
                return JsonResponse({"error": "Results directory is unavailable"}, status=503)

        warnings = tree_warnings(root_dir)
        _ensure_watcher(root_dir)
        cache_key = (
            root_dir,
            _max_tree_depth(),
            _max_inspected_files(),
            _scan_time_budget(),
        )
        now = time.time()
        if (
            _file_tree_cache["data"] is not None
            and _file_tree_cache["key"] == cache_key
            and now - _file_tree_cache["timestamp"] < _CACHE_TTL
        ):
            cached = _file_tree_cache["data"]
            return JsonResponse(
                {
                    "tree": cached["tree"],
                    "warnings": warnings,
                    "truncated": cached["meta"]["truncated"],
                    "reason": cached["meta"]["reason"],
                    "inspected": cached["meta"]["inspected"],
                    "elapsed_seconds": cached["meta"]["elapsed_seconds"],
                    "configured": True,
                }
            )

        tree, meta = _scan_results_dir(
            root_dir,
            max_depth=_max_tree_depth(),
            max_files=_max_inspected_files(),
            time_budget=_scan_time_budget(),
        )
        if meta["truncated"]:
            logger.warning(
                "Visualizer tree scan truncated (reason=%s, inspected=%d, elapsed=%.2fs) "
                "under %r — raise VISUALIZER_MAX_INSPECTED_FILES / "
                "VISUALIZER_SCAN_TIME_BUDGET_S / VISUALIZER_MAX_TREE_DEPTH if needed.",
                meta["reason"],
                meta["inspected"],
                meta["elapsed_seconds"],
                root_dir,
            )

        _file_tree_cache["data"] = {"tree": tree, "meta": meta}
        _file_tree_cache["timestamp"] = now
        _file_tree_cache["key"] = cache_key

        return JsonResponse(
            {
                "tree": tree,
                "warnings": warnings,
                "truncated": meta["truncated"],
                "reason": meta["reason"],
                "inspected": meta["inspected"],
                "elapsed_seconds": meta["elapsed_seconds"],
                "configured": True,
            }
        )


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
