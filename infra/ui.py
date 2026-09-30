"""Server-rendered UI — Django CBVs + Forms + HTMX."""
import csv
import hashlib
import hmac
import io
import itertools
import json
import logging
import os

from django.contrib import messages
from django.contrib.auth import login, logout
from django.contrib.auth.forms import AuthenticationForm
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db.models import Count, F, Max, ProtectedError, Q, RestrictedError
from django.http import Http404, HttpResponse, HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.generic import DetailView, TemplateView, View

from accounts import workos_auth
from accounts.models import User
from audits.comparison import compare_runs
from audits.events import ScenarioResult
from audits.models import AuditRun
from audits.services import create_audit_run, frozen_name, submit_audit_run
from infra.chat_feature import chat_enabled
from infra.hashing import scenario_revision_hash
from scenarios.models import (
    Scenario,
    ScenarioRevision,
    ScenarioSet,
    ScenarioSetVersion,
    ScenarioSetVersionItem,
)
from scenarios.services import publish_scenario_set_version

logger = logging.getLogger(__name__)


# ─── Mixins ──────────────────────────────────────────────────────────────────

class ProjectMixin(LoginRequiredMixin):
    """Scope all queries to request.project (set by ProjectMiddleware)."""


class AdminRequiredMixin(LoginRequiredMixin):
    """Restrict a view to superusers or users holding an ADMIN membership."""

    def dispatch(self, request, *args, **kwargs):
        # AnonymousUser has no .id; let LoginRequiredMixin handle the redirect
        # rather than querying the ORM with a lazy object.
        if not request.user.is_authenticated:
            return super().dispatch(request, *args, **kwargs)

        from accounts.services import is_any_project_admin

        if not is_any_project_admin(request.user):
            return HttpResponseForbidden("Admin access required.")
        return super().dispatch(request, *args, **kwargs)


class SuperuserRequiredMixin(LoginRequiredMixin):
    """Restrict a view to superusers only."""

    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return super().dispatch(request, *args, **kwargs)
        if not request.user.is_superuser:
            return HttpResponseForbidden("Super admin access required.")
        return super().dispatch(request, *args, **kwargs)


def write_block_reason(request) -> str | None:
    """Why this request may not change workspace data, or None when it may.

    Viewers are blocked (admin or auditor role required, the same rule as the
    API), and so is everyone but superusers in an archived workspace.
    """
    from audits.monitors import has_write_role

    project = getattr(request, "project", None)
    if project is None:
        return None
    if project.archived and not request.user.is_superuser:
        return "This workspace is archived and read-only."
    if not has_write_role(request.user, project):
        return "Admin or auditor role required to make changes in this workspace."
    return None


def _int(value, default: int = 0) -> int:
    """``value`` as an int (query and form values), ``default`` when it isn't one."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _require_write_access(request):
    """Guard for UI forms that change data: redirect back with an error when blocked, else None."""
    reason = write_block_reason(request)
    if reason:
        messages.error(request, reason)
        return redirect(request.META.get("HTTP_REFERER") or "/")
    return None


# ─── Health panel ────────────────────────────────────────────────────────────

class HealthView(AdminRequiredMixin, TemplateView):
    """System health dashboard. Initial snapshot is server-rendered; the page
    then polls /api/health/ every few seconds for live updates."""

    template_name = "health.html"

    def get_context_data(self, **kw):
        from infra.health import collect_health

        kw.setdefault("health", collect_health())
        return super().get_context_data(**kw)


# ─── Auth ────────────────────────────────────────────────────────────────────

class LoginView(TemplateView):
    template_name = "auth/login.html"

    def get(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            return redirect("dashboard")
        return super().get(request, *args, **kwargs)

    def get_context_data(self, **kw):
        kw.setdefault("form", AuthenticationForm())
        from django.conf import settings
        if getattr(settings, "DEMO_MODE", False):
            kw["demo_mode"] = True
            kw["demo_username"] = settings.DEMO_USERNAME
            kw["demo_password"] = settings.DEMO_PASSWORD
        kw["workos_enabled"] = getattr(settings, "WORKOS_ENABLED", False)
        return super().get_context_data(**kw)

    def post(self, request, *args, **kwargs):
        form = AuthenticationForm(request, data=request.POST)
        if form.is_valid():
            login(request, form.get_user())
            next_url = request.GET.get("next") or request.POST.get("next") or ""
            if next_url and next_url.startswith("/") and not next_url.startswith("//"):
                return redirect(next_url)
            return redirect("dashboard")
        return self.render_to_response(self.get_context_data(form=form))


class RegisterView(TemplateView):
    template_name = "auth/register.html"

    def get(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            return redirect("dashboard")
        return super().get(request, *args, **kwargs)

    def post(self, request, *args, **kwargs):
        from accounts.models import User
        username = request.POST.get("username", "").strip()
        password = request.POST.get("password", "")
        email = request.POST.get("email", "").strip()
        error = None
        if not username or not password:
            error = "Username and password are required."
        elif User.objects.filter(username=username).exists():
            error = "Username already taken."
        else:
            user = User.objects.create_user(username=username, password=password, email=email)
            from accounts.services import grant_default_project

            grant_default_project(user)
            login(request, user)
            return redirect("dashboard")
        return self.render_to_response(self.get_context_data(error=error))


def logout_view(request):
    logout(request)
    return redirect("login")


def auto_login_view(request):
    """One-click, one-time sign-in for the local one-liner demo.

    The CLI generates a single-use token at startup, prints it, and opens
    ``/auto-login/?token=...`` in the default browser. The token is checked in
    constant time and consumed on first use, so the URL cannot be replayed by
    another machine on the LAN (the demo server binds 0.0.0.0).
    Only enabled in MINIMAL_CONFIG (local demo) mode — 404 everywhere else.
    """
    from django.conf import settings
    from django.http import Http404

    if not getattr(settings, "MINIMAL_CONFIG", False):
        raise Http404
    token = request.GET.get("token", "")
    expected = os.environ.get("SIMPLEAUDIT_AUTO_LOGIN_TOKEN", "")
    if not expected or not hmac.compare_digest(token, expected):
        raise Http404
    # Single use: clear it so the URL stops working after this request.
    os.environ.pop("SIMPLEAUDIT_AUTO_LOGIN_TOKEN", None)
    username = os.environ.get("BOOTSTRAP_USERNAME", "studio")
    user = User.objects.filter(username=username).first()
    if user is None:
        raise Http404
    login(request, user)
    return redirect("dashboard")


# ─── WorkOS AuthKit ──────────────────────────────────────────────────────────

class WorkOSLoginView(TemplateView):
    """Step 1: user enters their email. We send a Magic Auth code via WorkOS.

    This uses the WorkOS User Management API directly (create_magic_auth)
    rather than the hosted AuthKit UI, which has staging-environment
    limitations (invalid-connection-selector). The API path works reliably
    in both staging and production.
    """

    template_name = "auth/workos_login.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["error"] = self.request.GET.get("error", "")
        return ctx

    def post(self, request):
        from django.conf import settings

        if not settings.WORKOS_ENABLED:
            messages.error(request, "WorkOS sign-in is not configured.")
            return redirect("login")
        email = request.POST.get("email", "").strip().lower()
        if not email or "@" not in email:
            return self.render_to_response(self.get_context_data(error="Please enter a valid email address."))
        try:
            workos_auth.send_magic_auth_code(
                email,
                ip_address=request.META.get("REMOTE_ADDR"),
                user_agent=request.META.get("HTTP_USER_AGENT", ""),
            )
        except Exception as exc:
            logger.exception("WorkOS magic auth send failed")
            return self.render_to_response(self.get_context_data(error=f"Could not send code: {exc}"))
        request.session["workos_email"] = email
        return redirect("workos_verify")


class WorkOSVerifyView(TemplateView):
    """Step 2: user enters the 6-digit code. We authenticate and log them in."""

    template_name = "auth/workos_verify.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["email"] = self.request.session.get("workos_email", "")
        ctx["error"] = self.request.GET.get("error", "")
        return ctx

    def post(self, request):
        from django.conf import settings

        if not settings.WORKOS_ENABLED:
            messages.error(request, "WorkOS sign-in is not configured.")
            return redirect("login")
        email = request.session.get("workos_email", "")
        code = request.POST.get("code", "").strip()
        if not email or not code:
            return self.render_to_response(self.get_context_data(error="Missing email or code."))
        try:
            user, created = workos_auth.authenticate_magic_auth(
                code,
                email,
                ip_address=request.META.get("REMOTE_ADDR"),
                user_agent=request.META.get("HTTP_USER_AGENT", ""),
            )
        except Exception as exc:
            logger.exception("WorkOS magic auth verification failed")
            return self.render_to_response(self.get_context_data(error=f"Verification failed: {exc}"))

        request.session.pop("workos_email", None)
        login(request, user)
        if created:
            _grant_default_project(user)
            messages.success(request, "Welcome! Your account was created via WorkOS sign-in.")
        else:
            messages.success(request, "Signed in with WorkOS.")
        return redirect("dashboard")


def _grant_default_project(user):
    """Give first-time WorkOS users membership in the 'Default' project (viewer).

    Thin wrapper over the shared ``grant_default_project`` service so every
    user-creation path lands new users in the same shared landing space.
    """
    from accounts.services import grant_default_project

    grant_default_project(user)


# ─── Dashboard ───────────────────────────────────────────────────────────────

class DashboardView(ProjectMixin, TemplateView):
    """Runs dashboard: stat cards + an interactive grid (rows load from RunsDataView)."""

    template_name = "dashboard.html"

    def get_context_data(self, **kw):
        from audits.models import Experiment
        from model_registry.models import RegisteredModel

        p = self.request.project
        base = AuditRun.objects.filter(project=p)
        ctx = super().get_context_data(**kw)
        live = Q(archived=False)
        ctx["stats"] = base.aggregate(   # one query for all stat cards
            total=Count("id", filter=live),
            active=Count("id", filter=live & ~Q(status__in=AuditRun.TERMINAL_STATUSES)),
            completed=Count("id", filter=live & Q(status=AuditRun.Status.COMPLETED)),
            failed=Count("id", filter=live & Q(status=AuditRun.Status.FAILED)),
            cancelled=Count("id", filter=live & Q(status=AuditRun.Status.CANCELLED)),
            archived=Count("id", filter=Q(archived=True)),
        )
        # Filter options as [{id, name}] — only values that appear in this workspace's runs.
        ctx["filter_targets"] = (
            RegisteredModel.objects.filter(target_audit_runs__project=p).distinct().order_by("display_name")
            .values("id", name=F("display_name"))
        )
        ctx["filter_sets"] = (
            ScenarioSet.objects.filter(project=p, versions__audit_runs__isnull=False).distinct().order_by("name")
            .values("id", "name")
        )
        from judges.models import Judge

        ctx["filter_judges"] = (
            Judge.objects.filter(project=p, versions__audit_runs__isnull=False).distinct().order_by("name").values("id", "name")
        )
        ctx["filter_experiments"] = Experiment.objects.filter(project=p).order_by("-created_at").values("id", "name")[:200]
        ctx["column_layout"] = (self.request.user.preferences or {}).get("dashboard_columns")
        return ctx


# ─── Workspaces ──────────────────────────────────────────────────────────────


class WorkspacesView(LoginRequiredMixin, TemplateView):
    """Workspace overview + team management.

    Every authenticated user sees the workspaces they belong to (superusers see
    all). Admins of a workspace can manage its team and delete it; the page
    degrades to read-only for viewers.
    """

    template_name = "workspaces.html"

    def get_context_data(self, **kw):
        from django.db.models import Q

        from accounts.models import Project, ProjectMembership
        from accounts.services import DEFAULT_PROJECT_SLUG

        user = self.request.user
        if user.is_superuser:
            projects = Project.objects.all()
        else:
            projects = Project.objects.filter(
                Q(memberships__user=user) | Q(slug=DEFAULT_PROJECT_SLUG)
            ).distinct()
        projects = list(projects.order_by("name"))

        roles = {}
        if not user.is_superuser:
            roles = dict(
                ProjectMembership.objects.filter(project__in=projects, user=user).values_list("project_id", "role")
            )
        from django.db.models import Count

        member_counts = dict(
            ProjectMembership.objects.filter(project__in=projects)
            .values("project_id")
            .annotate(n=Count("id"))
            .values_list("project_id", "n")
        )

        current_id = self.request.session.get("active_project_id")
        cards = []
        for project in projects:
            is_admin = user.is_superuser or roles.get(project.id) == ProjectMembership.Role.ADMIN
            cards.append(
                {
                    "project": project,
                    "is_admin": is_admin,
                    "is_current": project.id == current_id,
                    "member_count": member_counts.get(project.id, 0),
                }
            )

        # Manage panel: ?manage=<id>, only rendered with controls for admins.
        manage_id = self.request.GET.get("manage")
        managed = None
        if manage_id and manage_id.isdigit():
            candidate = next((c for c in cards if c["project"].id == int(manage_id)), None)
            if candidate:
                memberships = candidate["project"].memberships.select_related("user").order_by("created_at")
                managed = {
                    "workspace": candidate,
                    "memberships": memberships,
                    "can_manage": candidate["is_admin"],
                }

        kw["workspace_cards"] = cards
        kw["managed"] = managed
        kw["current_workspace"] = self.request.project
        # Any authenticated user may create a workspace (matches the API, which
        # allows self-service bootstrap without an existing admin).
        kw["can_create"] = True
        return super().get_context_data(**kw)


# ─── Super admin ─────────────────────────────────────────────────────────────


class AdminView(SuperuserRequiredMixin, TemplateView):
    """Platform administration: aggregate stats, workspace management, user
    management. Superusers only. Counts only — no workspace content."""

    template_name = "admin.html"

    def get_context_data(self, **kw):
        from accounts.models import Project, User
        from accounts.services import admin_stats_payload, workspace_has_content

        tab = self.request.GET.get("tab", "overview")
        if tab not in ("overview", "workspaces", "users"):
            tab = "overview"

        stats = admin_stats_payload()

        # Workspaces tab: per-workspace content flag + members for the panel.
        projects = list(Project.objects.order_by("name"))
        for ws in stats["workspaces"]:
            project = next((p for p in projects if p.id == ws["id"]), None)
            ws["has_content"] = workspace_has_content(project) if project else False
        manage_id = self.request.GET.get("manage")
        managed_members = None
        if tab == "workspaces" and manage_id and manage_id.isdigit():
            project = next((p for p in projects if p.id == int(manage_id)), None)
            if project:
                managed_members = list(
                    project.memberships.select_related("user").order_by("created_at")
                )

        # Users tab.
        users = list(User.objects.order_by("username"))
        superuser_count = sum(1 for u in users if u.is_superuser)

        kw.update(
            tab=tab,
            stats=stats,
            workspaces=stats["workspaces"],
            users=users,
            superuser_count=superuser_count,
            managed_members=managed_members,
            manage_id=int(manage_id) if manage_id and manage_id.isdigit() else None,
        )
        return super().get_context_data(**kw)


# ─── Profile ─────────────────────────────────────────────────────────────────


class ProfileView(LoginRequiredMixin, TemplateView):
    """Self-service profile update for the signed-in user."""

    template_name = "profile.html"

    def get_context_data(self, **kw):
        from accounts.services import has_local_password

        user = self.request.user
        kw["profile_user"] = user
        kw["has_local_password"] = has_local_password(user)
        kw["is_sso"] = bool(user.workos_user_id)
        kw.setdefault("error", None)
        return super().get_context_data(**kw)

    def post(self, request, *args, **kwargs):
        from django.contrib.auth import update_session_auth_hash
        from django.contrib.auth.password_validation import validate_password
        from django.core.exceptions import ValidationError as DjangoValidationError

        from accounts.services import has_local_password

        user = request.user
        form = request.POST.get("form", "details")
        error = None

        if form == "password":
            current_password = request.POST.get("current_password") or ""
            new_password = request.POST.get("new_password") or ""
            if not new_password:
                error = "Enter a new password."
            elif has_local_password(user) and (
                not current_password or not user.check_password(current_password)
            ):
                # SSO users (and anyone without a local password) have nothing
                # to verify against, so they set a password directly.
                error = "Current password is incorrect."
            else:
                try:
                    validate_password(new_password)
                except DjangoValidationError as exc:
                    error = " ".join(exc.messages)
            if error:
                return self.render_to_response(self.get_context_data(error=error))
            user.set_password(new_password)
            user.save(update_fields=["password"])
            update_session_auth_hash(request, user)
            messages.success(request, "Password updated.")
            return redirect("profile")

        first_name = (request.POST.get("first_name") or "").strip()
        last_name = (request.POST.get("last_name") or "").strip()
        email = (request.POST.get("email") or "").strip()
        username = (request.POST.get("username") or "").strip()

        if not username:
            error = "Username cannot be empty."
        elif User.objects.filter(username__iexact=username).exclude(pk=user.pk).exists():
            error = "Username already exists."
        elif email and User.objects.filter(email__iexact=email).exclude(pk=user.pk).exists():
            error = "Email already exists."

        if error:
            return self.render_to_response(self.get_context_data(error=error))

        user.first_name = first_name
        user.last_name = last_name
        user.email = email
        user.username = username
        user.save()
        messages.success(request, "Profile updated.")
        return redirect("profile")


# ─── New Audit ───────────────────────────────────────────────────────────────

def _run_from_query(request, param: str):
    """The active project's run named by ``?<param>=<id>``, or None."""
    raw = request.GET.get(param) or ""
    if not raw.isdigit():
        return None
    return (
        AuditRun.objects.filter(id=int(raw), project=request.project)
        .select_related(
            "scenario_set_version__scenario_set", "target_model", "auditor_model", "judge_model"
        )
        .first()
    )


# Generation keys with their own fields on the run settings form.
_SETTINGS_FORM_KEYS = {"max_turns", "n_repetitions", "language", "system_prompt"}


def _clone_from_run(source: AuditRun) -> dict:
    """Prefill for the shared audit form blocks from an existing run."""
    params = source.generation_parameters_snapshot or {}
    # Strip form-managed keys from the JSON so they don't appear in the Advanced
    # textarea (they're pre-filled in their own fields).
    gen_params = {k: v for k, v in params.items() if k not in _SETTINGS_FORM_KEYS}
    return {
        "scenario_set_id": source.scenario_set_version.scenario_set_id,
        "scenario_set_version_id": source.scenario_set_version_id,
        "target_model_id": source.target_model_id,
        "auditor_model_id": source.auditor_model_id,
        "judge_model_id": source.judge_model_id,
        "judge_version_id": source.judge_version_id,
        "max_turns": params.get("max_turns", ""),
        "language": params.get("language", ""),
        "n_repetitions": params.get("n_repetitions", ""),
        "system_prompt": params.get("system_prompt", ""),
        "generation_json": json.dumps(gen_params, indent=2, sort_keys=True) if gen_params else "",
    }


def _design_selection(post=None, clone=None) -> dict:
    """What the design form should show as selected: from a POST, a cloned run, or empty.

    ``versions``: ticked scenario set version ids; a set id in ``sets`` means
    "its latest version" (the default tick).
    """
    if post is not None:
        return {
            "sets": post.getlist("scenario_set"),
            "versions": post.getlist("scenario_version"),
            "target": post.getlist("target_model"),
            "auditor": post.getlist("auditor_model"),
            "judge_model": post.getlist("judge_model"),
            "judge": post.getlist("judge"),
        }
    if clone:
        # Clone pins the cloned run's exact version.
        return {
            "sets": [],
            "versions": [str(clone["scenario_set_version_id"])],
            "target": [str(clone["target_model_id"])],
            "auditor": [str(clone["auditor_model_id"])],
            "judge_model": [str(clone["judge_model_id"])],
            "judge": [str(clone["judge_version_id"])],
        }
    return {"sets": [], "versions": [], "target": [], "auditor": [], "judge_model": [], "judge": []}


def ex_repeat(repeat: dict) -> str:
    from audits.monitors import repeat_label

    return repeat_label(repeat)


def ex_label(spec: dict) -> str:
    """'Health v2' style label of a spec's scenario set version (monitor names)."""
    from audits.experiments import factor_value_label

    return factor_value_label("scenario_set", spec["scenario_set"])


def _design_from_review(post):
    """The original design, carried through the review screen as design__<field>."""
    from django.http import QueryDict

    design = QueryDict(mutable=True)
    for key, values in post.lists():
        if key.startswith("design__"):
            design.setlist(key[len("design__"):], values)
    return design


def _settings_prefill(post) -> dict:
    """Re-fill the settings block (same shape as a clone) from a POST."""
    return {
        "max_turns": post.get("max_turns", ""),
        "language": post.get("language", ""),
        "n_repetitions": post.get("n_repetitions", ""),
        "system_prompt": post.get("system_prompt", ""),
        "generation_json": post.get("gen_config_json", ""),
    }


class NewExperimentView(ProjectMixin, TemplateView):
    """New Experiment: design (every input takes one or more values) → review → launch.

    A design that expands to one run launches it straight away. "Repeat" (other
    than Once) also creates one Monitor per run setup.
    """

    template_name = "experiment_new.html"

    def get_context_data(self, **kw):
        from audits.experiments import MAX_RUNS_PER_EXPERIMENT
        from audits.monitors import has_write_role
        from model_registry.services import (
            connection_share_label,
            visible_connections_for,
        )

        p = self.request.project
        source = _run_from_query(self.request, "clone_from")
        clone = _clone_from_run(source) if source else None
        kw.setdefault("error", None)
        kw.setdefault("clone", clone)
        kw.setdefault("sel", _design_selection(clone=clone))
        sel = kw["sel"]
        # Scenario sets with their versions (newest first) and which are ticked:
        # a set id in sel["sets"] means its latest version.
        from django.db.models import Prefetch

        sets = list(
            ScenarioSet.objects.filter(project=p)
            .order_by("name")
            .prefetch_related(
                Prefetch("versions", queryset=ScenarioSetVersion.objects.select_related("published_by").order_by("-version"))
            )
        )
        for sset in sets:
            sset.version_list = list(sset.versions.all())
            latest_id = str(sset.version_list[0].id) if sset.version_list else None
            sset.follow_value = f"latest:{sset.id}"
            sset.ticked = {str(v.id) for v in sset.version_list if str(v.id) in sel["versions"]}
            if sset.follow_value in sel["versions"]:
                sset.ticked.add(sset.follow_value)
            if latest_id and str(sset.id) in sel["sets"]:
                sset.ticked.add(latest_id)
            # Open the version list when anything but the current version is ticked.
            sset.show_versions = bool(sset.ticked - {latest_id})
        # Repeat prefill: a POST being re-shown, else ?repeat= (e.g. "Monitor for drift").
        from audits.monitors import REPEAT_CHOICES

        get = self.request.GET
        kw.setdefault("rep", {
            "repeat": get.get("repeat", "once"),
            "timezone": "",
            "cron_expression": "",
            "start": "baseline" if source and get.get("repeat") else "now",
            "first_run_at": "",
            "clone_from": str(source.id) if source else "",
        })
        # Connections the picker offers: this workspace's own plus any shared
        # into it (public / admin-shared / explicitly shared). Each carries a
        # sharing label so users can tell at a glance where a model comes from.
        connections = visible_connections_for(self.request.user, p)
        for conn in connections:
            conn.share_label = connection_share_label(conn)
            conn.is_shared = conn.project_id != p.id
        kw.update(
            sets=sets,
            connections=connections,
            model_roles=[
                ("target", "Target", "The model under test.", sel["target"]),
                ("auditor", "Auditor", "Plays the user and probes the target.", sel["auditor"]),
                ("judge", "Judge model", "Grades each conversation.", sel["judge_model"]),
            ],
            judges=_judge_picker(p, sel["judge"]),
            max_runs=MAX_RUNS_PER_EXPERIMENT,
            repeat_choices=REPEAT_CHOICES,
            timezones=_timezone_choices(),
            source_run=source,
            # Same rule as launching a run: admin or auditor (superusers too).
            can_launch=has_write_role(self.request.user, p),
        )
        return super().get_context_data(**kw)

    def _redesign(self, post, error=None):
        rep = {k: post.get(k, "") for k in ("repeat", "timezone", "cron_expression", "start", "first_run_at", "clone_from",
                                            "name")}
        rep["repeat"] = rep["repeat"] or "once"
        source = None
        if rep["clone_from"].isdigit():
            source = AuditRun.objects.filter(pk=int(rep["clone_from"]), project=self.request.project).first()
        return self.render_to_response(self.get_context_data(
            error=error, sel=_design_selection(post=post), clone=_settings_prefill(post), rep=rep,
            source_run=source,
        ))

    def post(self, request, *args, **kwargs):
        from audits import experiments as ex

        blocked = _require_write_access(request)
        if blocked:
            return blocked
        action = request.POST.get("action", "design")
        if action == "edit":
            design = _design_from_review(request.POST)
            if "experiment_name" in request.POST:   # a name edited on review comes back too
                design["name"] = request.POST["experiment_name"]
            return self._redesign(design)
        if action == "launch":
            return self._launch(request)
        from audits.monitors import parse_repeat

        try:
            design = ex.parse_design(request.POST, request.project)
            specs = ex.expand(design)
            repeat = parse_repeat(request.POST)
        except ValueError as e:  # DesignError is a ValueError too
            return self._redesign(request.POST, str(e))
        if len(specs) == 1:
            return self._launch_single(request, specs[0], repeat)
        return self._review(request, design, specs, repeat)

    def _launch_single(self, request, spec, repeat):
        """One run setup: launch it (unless starting later) and, if repeating, monitor it."""
        from django.db import transaction

        from audits.experiments import spec_to_run
        from audits.monitors import create_monitor

        p = request.project
        run_spec = spec_to_run(spec, name="")
        custom_name = request.POST.get("name", "").strip()[:250]
        run = monitor = None
        try:
            with transaction.atomic():
                if not repeat or repeat["start"] == "now":
                    run = create_audit_run(
                        project=p,
                        user=request.user,
                        name=custom_name or f"Run {timezone.now():%Y-%m-%d %H:%M}",
                        scenario_set_version=run_spec["version"],
                        target_model=run_spec["target"],
                        auditor_model=run_spec["auditor"],
                        judge_model=run_spec["judge_model"],
                        judge=run_spec["judge"],
                        max_turns_override=run_spec["max_turns"],
                        language_override=run_spec["language"],
                        n_repetitions_override=run_spec["n_repetitions"],
                        gen_config_override=run_spec["gen_config"],
                    )
                if repeat:
                    first_point = run
                    if repeat["start"] == "baseline" and repeat["baseline_run"].isdigit():
                        first_point = AuditRun.objects.filter(pk=int(repeat["baseline_run"]), project=p).first()
                    monitor = create_monitor(
                        project=p,
                        user=request.user,
                        name=custom_name or f"{spec['target'].display_name} · {ex_label(spec)}",
                        run=run_spec,
                        repeat=repeat,
                        first_point=first_point,
                    )
        except Exception as e:  # noqa: BLE001 - surface any creation failure to the user
            return self._redesign(request.POST, str(e))
        if run:
            submit_audit_run(run)
        if monitor:
            messages.success(request, f"Monitor “{monitor.name}” created: next run {monitor.next_run_at:%Y-%m-%d %H:%M} UTC.")
        return redirect(f"/runs/{run.id}/" if run else f"/monitors/{monitor.id}/")

    # --- Review -------------------------------------------------------------
    # Rows carry the fixed part of a run (scenario version + models) as a JSON
    # "spec", plus editable fields: name, max turns, language, repetitions and
    # generation config. Rows can be duplicated client-side, so indices may
    # have gaps; the server walks 0..row_count and skips missing ones.

    @staticmethod
    def _row_context(i, spec_objs, *, name, max_turns, language, n_reps, system_prompt, gen_json, include=True,
                     duplicate=False):
        from audits import experiments as ex

        spec = {"scenario_set": spec_objs["version"], **{r: spec_objs[r] for r in ("target", "auditor", "judge_model", "judge")}}
        return {
            "i": i,
            "spec": ex.spec_to_row(spec),
            "name": name,
            "values": {k: ex.factor_value_label(k, spec[k]) for k in ("scenario_set", "target", "auditor", "judge_model", "judge")},
            "max_turns": max_turns or "",
            "language": language or "",
            "n_repetitions": n_reps or "",
            "system_prompt": system_prompt or "",
            "gen_config_json": gen_json,
            "warnings": ex.spec_warnings(spec),
            "include": include,
            "duplicate": duplicate,
        }

    def _render_review(self, request, *, rows, experiment_name, design_post, repeat=None, error=None):
        from audits import experiments as ex
        from audits.monitors import repeat_label, zone

        # Columns: the scenario set / model roles that differ between rows.
        columns = [
            (key, label)
            for key, label in ex.DESIGN_AXES.items()
            if key in ("scenario_set", "target", "auditor", "judge_model", "judge") and len({r["values"][key] for r in rows}) > 1
        ]
        fixed = [
            (label, rows[0]["values"][key])
            for key, label in ex.DESIGN_AXES.items()
            if key in rows[0]["values"] and key not in dict(columns)
        ]
        warnings = ex.design_warnings([k for k, _ in columns])
        # A warning true for every run is shown once at the top, not per row.
        shared = set.intersection(*(set(r["warnings"]) for r in rows)) if len(rows) > 1 else set()
        warnings += [f"All runs: {w}" for w in rows[0]["warnings"] if w in shared] if rows else []
        for row in rows:
            row["cells"] = [row["values"][k] for k, _ in columns]
            row["warnings"] = [w for w in row["warnings"] if w not in shared]
        return render(request, "experiment_review.html", {
            "experiment_name": experiment_name,
            "columns": columns,
            "fixed": fixed,
            "rows": rows,
            "row_count": max((r["i"] for r in rows), default=-1) + 1,
            "design_warnings": warnings,
            "design_post": design_post,
            "repeat": repeat,
            "repeat_label": repeat_label(repeat),
            "repeat_first_local": repeat["first_run_at"].astimezone(zone(repeat["timezone"])) if repeat else None,
            "error": error,
            "max_runs": ex.MAX_RUNS_PER_EXPERIMENT,
        })

    def _review(self, request, design, specs, repeat=None):
        from audits import experiments as ex

        factors = ex.varied_factors(design)
        gen_config = dict(design["gen_config"] or {})
        system_prompt = gen_config.pop("system_prompt", "")
        gen_json = json.dumps(gen_config, indent=2, sort_keys=True) if gen_config else ""
        rows = [
            self._row_context(
                i,
                {"version": spec["scenario_set"], "target": spec["target"], "auditor": spec["auditor"],
                 "judge_model": spec["judge_model"], "judge": spec["judge"]},
                name=ex.spec_label(spec, factors),
                max_turns=spec["max_turns"],
                language=spec["language"],
                n_reps=spec["n_repetitions"],
                system_prompt=system_prompt,
                gen_json=gen_json,
            )
            for i, spec in enumerate(specs)
        ]
        design_post = [(k, v) for k, vs in request.POST.lists() if k not in ("csrfmiddlewaretoken", "action") for v in vs]
        return self._render_review(
            request, rows=rows,
            experiment_name=request.POST.get("name", "").strip()[:250] or ex.default_experiment_name(design, factors),
            design_post=design_post, repeat=repeat,
        )

    def _rows_from_post(self, post, project):
        """Rebuild review rows and the runs to launch from a review POST.

        Returns (rows, runs, errors). Every row is rebuilt (so the review can be
        re-shown with the user's edits); only ticked rows become runs.
        """
        import copy

        from judges.models import JudgeVersion
        from model_registry.models import RegisteredModel

        specs = {}
        for i in range(min(int(post.get("row_count") or 0), 500)):
            raw_spec = post.get(f"run-{i}-spec")
            if raw_spec:   # missing = removed duplicate
                specs[i] = json.loads(raw_spec)
        # Two queries for all rows; ids not usable in this workspace (and not
        # shared into it) are simply absent.
        from model_registry.services import visible_connection_ids_for

        allowed_conn_ids = set(visible_connection_ids_for(project))
        versions = ScenarioSetVersion.objects.select_related("scenario_set").filter(scenario_set__project=project).in_bulk(
            {spec["v"] for spec in specs.values()}
        )
        _model_ids = {spec[k] for spec in specs.values() for k in ("t", "a", "jm")}
        # Keyed by int pk to match the int ids in each row's spec (and what the
        # previous in_bulk() returned); a str key would never match on lookup.
        models = {
            m.pk: m
            for m in RegisteredModel.objects.select_related("connection").filter(pk__in=_model_ids)
            if m.project_id == project.id or m.connection_id in allowed_conn_ids
        }
        judges = JudgeVersion.objects.select_related("judge").filter(
            judge__project=project
        ).in_bulk({spec["j"] for spec in specs.values()})

        def lookup(found, pk, model):
            if pk not in found:
                raise model.DoesNotExist(f"{model.__name__} {pk} is not in this workspace.")
            return found[pk]

        rows, runs, errors = [], [], []
        for i, spec in specs.items():
            version = lookup(versions, spec["v"], ScenarioSetVersion)
            if spec.get("f"):
                version = copy.copy(version)   # the flag is per row
                version.follow_latest = True
            judge = lookup(judges, spec["j"], JudgeVersion)
            if spec.get("jf"):
                judge = copy.copy(judge)   # the flag is per row
                judge.follow_latest = True
            objs = {
                "version": version,
                "target": lookup(models, spec["t"], RegisteredModel),
                "auditor": lookup(models, spec["a"], RegisteredModel),
                "judge_model": lookup(models, spec["jm"], RegisteredModel),
                "judge": judge,
            }
            name = (post.get(f"run-{i}-name") or "").strip() or f"Run {i + 1}"
            mt = (post.get(f"run-{i}-max_turns") or "").strip()
            lang = (post.get(f"run-{i}-language") or "").strip()
            reps = (post.get(f"run-{i}-n_repetitions") or "").strip()
            system_prompt = (post.get(f"run-{i}-system_prompt") or "").strip()
            gen_raw = (post.get(f"run-{i}-gen_config_json") or "").strip()
            include = bool(post.get(f"run-{i}-include"))
            rows.append(self._row_context(
                i, objs, name=name, max_turns=mt, language=lang, n_reps=reps, system_prompt=system_prompt,
                gen_json=gen_raw,
                include=include, duplicate=bool(post.get(f"run-{i}-duplicate")),
            ))
            if not include:
                continue
            try:
                max_turns = int(mt) if mt else None
                if max_turns is not None and not 1 <= max_turns <= 50:
                    raise ValueError
            except ValueError:
                errors.append(f"“{name}”: max turns must be a whole number from 1 to 50.")
                continue
            try:
                n_reps = int(reps) if reps else None
                if n_reps is not None and not 1 <= n_reps <= 20:
                    raise ValueError
            except ValueError:
                errors.append(f"“{name}”: repetitions must be a whole number from 1 to 20.")
                continue
            gen_config = None
            if gen_raw and gen_raw != "{}":
                try:
                    gen_config = json.loads(gen_raw)
                    if not isinstance(gen_config, dict):
                        raise TypeError("must be a JSON object")
                except (ValueError, TypeError) as exc:
                    errors.append(f"“{name}”: generation config is not valid JSON ({exc}).")
                    continue
            if system_prompt:
                gen_config = {**(gen_config or {}), "system_prompt": system_prompt}
            elif gen_config:
                gen_config.pop("system_prompt", None)   # cleared on review
            runs.append({
                "name": name,
                **objs,
                "max_turns": max_turns,
                "language": lang or None,
                "n_repetitions": n_reps if n_reps and n_reps > 1 else None,
                "gen_config": gen_config or None,
            })
        return rows, runs, errors

    def _launch(self, request):
        from audits import experiments as ex
        from audits.monitors import parse_repeat

        p = request.project
        post = request.POST
        design_post = [(k[len("design__"):], v) for k, vs in post.lists() if k.startswith("design__") for v in vs]
        design = _design_from_review(post)
        try:
            rows, runs, errors = self._rows_from_post(post, p)
        except Exception as e:  # noqa: BLE001 - tampered or stale form: start again from the design
            return self._redesign(design, f"Could not read the review: {e}")
        try:
            repeat = parse_repeat(design)
        except ValueError as e:
            repeat = None
            errors.append(str(e))
        name = post.get("experiment_name", "")
        if not errors:
            try:
                experiment = ex.launch_experiment(project=p, user=request.user, name=name, runs=runs, repeat=repeat)
            except Exception as e:  # noqa: BLE001 - show the reason on the review screen
                errors.append(str(e))
            else:
                if repeat:
                    messages.success(request, f"Experiment launched; its {len(runs)} run setups now repeat {ex_repeat(repeat)}.")
                return redirect(f"/experiments/{experiment.id}/")
        if not rows:
            return self._redesign(design, " ".join(errors))
        return self._render_review(
            request, rows=rows, experiment_name=name, design_post=design_post, repeat=repeat, error=" ".join(errors)
        )


# ─── Experiments ─────────────────────────────────────────────────────────────



def _experiment_summary(runs) -> dict:
    total = len(runs)
    done = sum(1 for r in runs if not r.is_active)
    failed = sum(1 for r in runs if r.status == "failed")
    return {
        "total": total,
        "done": done,
        "failed": failed,
        "active": total - done,
        "pct": done * 100 // total if total else 0,
    }


class ExperimentsView(ProjectMixin, TemplateView):
    template_name = "experiments.html"

    def get_context_data(self, **kw):
        from audits.experiments import FACTORS
        from audits.models import Experiment

        experiments = list(
            Experiment.objects.filter(project=self.request.project)
            .select_related("created_by")
            .prefetch_related("runs")
        )
        for e in experiments:
            e.summary = _experiment_summary(list(e.runs.all()))
            e.factor_labels = [FACTORS.get(f, f) for f in e.factors]
        kw["experiments"] = experiments
        return super().get_context_data(**kw)


class ExperimentDetailView(ProjectMixin, TemplateView):
    template_name = "experiment_detail.html"

    def get_context_data(self, **kw):
        from audits.experiments import FACTORS, experiment_pivot, run_factor_values
        from audits.models import Experiment
        from audits.monitors import can_manage_monitor, drift_series, pass_counts

        experiment = get_object_or_404(Experiment, pk=kw["experiment_id"], project=self.request.project)
        runs = list(
            experiment.runs.select_related(
                "scenario_set_version__scenario_set", "target_model", "auditor_model", "judge_model"
            ).order_by("created_at", "id")
        )
        factors = [f for f in experiment.factors if f in FACTORS]
        # Pivot axes from the query string, defaulting to the first two factors.
        row_factor = self.request.GET.get("rows") if self.request.GET.get("rows") in factors else None
        col_factor = self.request.GET.get("cols") if self.request.GET.get("cols") in factors else None
        if not row_factor and not col_factor:
            row_factor = factors[0] if factors else None
            col_factor = factors[1] if len(factors) > 1 else None
        if col_factor == row_factor:
            col_factor = None
        counts = pass_counts([r.id for r in runs])
        for r in runs:
            values = run_factor_values(r)
            r.factor_values = [values[f] for f in factors]
            c = counts[r.id]
            r.pass_rate = c["k"] * 100 / c["n"] if c["n"] else None
        # One "setup" per monitor (its repeats) or per standalone run. By
        # default each setup contributes its latest judged run, so repeats show
        # the current state instead of being pooled with old ones (which would
        # hide drift). ?pool=1 pools every run.
        pool = self.request.GET.get("pool") == "1"
        latest: dict = {}
        for r in runs:
            key = ("monitor", r.monitor_id) if r.monitor_id else ("run", r.id)
            if key not in latest or counts[r.id]["n"] or not counts[latest[key].id]["n"]:
                latest[key] = r
        current = sorted(latest.values(), key=lambda r: r.id)
        monitors = list(experiment.monitors.select_related("project", "created_by").order_by("id"))
        for m in monitors:
            points = drift_series(m)
            m.chart = _drift_chart(points, width=220, height=48, pad=4)
            judged = [pt for pt in points if pt["rate"] is not None]
            m.latest_point = judged[-1] if judged else None
            m.drops = sum(1 for pt in points if pt["change"] == "drop")
            m.can_manage = can_manage_monitor(self.request.user, m)
        kw.update(
            experiment=experiment,
            runs=runs,
            current_runs=current,
            has_repeats=len(current) < len(runs),
            pool=pool,
            factor_labels=[FACTORS[f] for f in factors],
            factor_choices=[(f, FACTORS[f]) for f in factors],
            row_factor=row_factor,
            col_factor=col_factor,
            row_label=FACTORS.get(row_factor, ""),
            col_label=FACTORS.get(col_factor, ""),
            pivot=experiment_pivot(runs if pool else current, row_factor, col_factor),
            summary=_experiment_summary(current),
            monitors=monitors,
            run_ids=",".join(str(r.id) for r in current),
        )
        return super().get_context_data(**kw)


# ─── Monitors (recurring runs / drift) ──────────────────────────────────────
# Monitors are created from New Experiment ("Repeat"); this page lists them.


def _judge_picker(project, selected: list[str]) -> list:
    """Judges for the New Experiment picker, each with its versions (newest first).

    ``selected``: ticked values (version ids, or "latest:<judge id>" for always
    latest). With nothing ticked, the first judge's latest version is ticked so
    a quick run needs no extra click.
    """
    from django.db.models import Prefetch

    from judges.models import Judge, JudgeVersion
    from judges.services import decorate, default_judge

    judges = list(
        Judge.objects.filter(project=project).order_by("name").prefetch_related(
            Prefetch("versions", queryset=JudgeVersion.objects.order_by("-version"))
        )
    )
    if not selected and judges and judges[0].versions.all():
        preferred = default_judge(project)
        default = next((j for j in judges if preferred and j.pk == preferred.pk), judges[0])
        selected = [str(default.versions.all()[0].id)]
    for judge in judges:
        judge.version_list = [decorate(v) for v in judge.versions.all()]
        judge.follow_value = f"latest:{judge.id}"
        judge.ticked = {str(v.id) for v in judge.version_list if str(v.id) in selected}
        if judge.follow_value in selected:
            judge.ticked.add(judge.follow_value)
        latest_id = str(judge.version_list[0].id) if judge.version_list else None
        judge.show_versions = bool(judge.ticked - {latest_id})
    return judges


def _timezone_choices() -> list[str]:
    """IANA zone names for the Repeat timezone picker (UTC first, then alphabetical)."""
    from zoneinfo import available_timezones

    zones = sorted(z for z in available_timezones() if "/" in z and not z.startswith(("Etc/", "SystemV/")))
    return ["UTC", *zones]


class MonitorsView(ProjectMixin, TemplateView):
    template_name = "monitors.html"

    def get_context_data(self, **kw):
        from audits.models import Monitor
        from audits.monitors import (
            MAX_MONITORS_PER_PROJECT,
            _role,
            can_manage_monitor,
            has_write_role,
        )

        p = self.request.project
        monitors = list(
            Monitor.objects.filter(project=p).select_related(
                "project", "scenario_set", "scenario_set_version", "target_model", "auditor_model", "judge_model", "judge", "judge_version",
                "last_run", "created_by", "experiment",
            )
        )
        role = _role(self.request.user, p)   # once, not per monitor
        for m in monitors:
            m.can_manage = can_manage_monitor(self.request.user, m, role=role)
        kw.update(
            monitors=monitors,
            can_create=has_write_role(self.request.user, p) and not (p.archived and not self.request.user.is_superuser),
            max_monitors=MAX_MONITORS_PER_PROJECT,
        )
        return super().get_context_data(**kw)


def _drift_chart(points: list[dict], width: int = 720, height: int = 220, pad: int = 32) -> dict:
    """Pre-computed SVG geometry for the pass-rate series with its 95% CI band."""
    plotted = [p for p in points if p["rate"] is not None]
    if not plotted:
        return {}
    inner_w, inner_h = width - 2 * pad, height - 2 * pad
    step = inner_w / max(len(plotted) - 1, 1)

    def y(v):
        return round(pad + (1 - v) * inner_h, 1)

    dots, upper, lower = [], [], []
    for i, p in enumerate(plotted):
        x = round(pad + (i * step if len(plotted) > 1 else inner_w / 2), 1)
        dots.append({"x": x, "y": y(p["rate"]), "p": p})
        upper.append(f"{x},{y(p['hi'])}")
        lower.append(f"{x},{y(p['lo'])}")
    return {
        "width": width,
        "height": height,
        "line": " ".join(f"{d['x']},{d['y']}" for d in dots),
        "band": " ".join(upper + lower[::-1]),
        "dots": dots,
        "grid": [{"y": y(v), "label": f"{int(v * 100)}%"} for v in (0, 0.25, 0.5, 0.75, 1)],
        "pad": pad,
        "right": width - pad,
    }


class MonitorDetailView(ProjectMixin, TemplateView):
    template_name = "monitor_detail.html"

    def get_context_data(self, **kw):
        from audits.models import Monitor
        from audits.monitors import BASELINE_WINDOW, can_manage_monitor, drift_series

        monitor = get_object_or_404(
            Monitor.objects.select_related(
                "scenario_set", "scenario_set_version", "target_model", "auditor_model", "judge_model", "judge", "judge_version"
            ),
            pk=kw["monitor_id"],
            project=self.request.project,
        )
        points = drift_series(monitor)
        for prev, cur in itertools.pairwise(points):
            cur["prev_run_id"] = prev["run"].id
        kw.update(
            monitor=monitor,
            points=list(reversed(points)),
            chart=_drift_chart(points),
            baseline_window=BASELINE_WINDOW,
            can_manage=can_manage_monitor(self.request.user, monitor),
            drops=sum(1 for p in points if p["change"] == "drop"),
        )
        return super().get_context_data(**kw)


class MonitorActionView(ProjectMixin, View):
    """POST /monitors/<id>/<action>/ — toggle | run-now | delete."""

    def post(self, request, monitor_id, action):
        from audits.models import Monitor
        from audits.monitors import (
            can_manage_monitor,
            launch_monitor,
            owner_authorized,
        )

        blocked = _require_write_access(request)
        if blocked:
            return blocked
        monitor = get_object_or_404(
            Monitor.objects.select_related("project", "created_by"), pk=monitor_id, project=request.project
        )
        if not can_manage_monitor(request.user, monitor):
            return HttpResponseForbidden("Only a workspace admin or the monitor's creator can do this.")
        if action == "toggle" and not monitor.enabled and not owner_authorized(monitor):
            messages.error(
                request,
                "Cannot resume: the monitor's creator no longer has admin or auditor role. Recreate it under your account.",
            )
        elif action == "toggle":
            monitor.enabled = not monitor.enabled
            if monitor.enabled and monitor.next_run_at < timezone.now():
                from audits.monitors import next_after

                monitor.next_run_at = next_after(monitor, timezone.now())
            monitor.save(update_fields=["enabled", "next_run_at", "updated_at"])
            messages.success(request, f"Monitor '{monitor.name}' {'resumed' if monitor.enabled else 'paused'}.")
        elif action == "run-now":
            last = monitor.last_run
            if last and last.status not in (AuditRun.Status.COMPLETED, AuditRun.Status.FAILED, AuditRun.Status.CANCELLED):
                messages.error(request, f"Run #{last.id} from this monitor is still {last.status}.")
            else:
                try:
                    run = launch_monitor(monitor)
                except Exception as e:  # noqa: BLE001 - surface creation failure to the user
                    messages.error(request, f"Could not launch: {e}")
                else:
                    monitor.last_run = run
                    monitor.last_error = ""
                    monitor.save(update_fields=["last_run", "last_error", "updated_at"])
                    submit_audit_run(run)
                    return redirect(f"/runs/{run.id}/")
        elif action == "delete":
            name = monitor.name
            monitor.delete()
            messages.success(request, f"Monitor '{name}' deleted. Its runs are kept.")
            return redirect("/monitors/")
        return redirect(request.META.get("HTTP_REFERER") or f"/monitors/{monitor_id}/")


# ─── Queue ───────────────────────────────────────────────────────────────────

# ─── Scenarios ───────────────────────────────────────────────────────────────

class ScenariosView(ProjectMixin, TemplateView):
    template_name = "scenarios.html"

    def get_context_data(self, **kw):
        p = self.request.project
        sets = ScenarioSet.objects.filter(project=p).annotate(
            version_count=Count("versions"), latest_version=Max("versions__version")).order_by("name")
        selected = None
        items = []
        versions = []
        viewing_version = None
        if self.request.GET.get("set"):
            selected = ScenarioSet.objects.filter(pk=self.request.GET["set"], project=p).first()
            if selected:
                versions = list(selected.versions.order_by("-version"))
                # Check if a specific version is being viewed
                ver_param = self.request.GET.get("version")
                if ver_param and ver_param.isdigit():
                    viewing_version = selected.versions.filter(version=int(ver_param)).first()
                if not viewing_version and versions:
                    viewing_version = versions[0]
                if viewing_version:
                    items = list(viewing_version.items.select_related("scenario", "revision"))
                    # For the latest version, hide archived scenarios (they're still in historical versions)
                    if viewing_version == versions[0]:
                        items = [i for i in items if i.scenario.archived_at is None]
        prev_version = (viewing_version.version - 1) if viewing_version and viewing_version.version > 1 else None
        # Collect unique categories for filter dropdown
        categories = sorted({i.scenario.category for i in items if i.scenario.category}) if items else []
        kw.update(sets=sets, selected=selected, items=items, versions=versions, viewing_version=viewing_version,
                  viewing_is_latest=bool(versions) and viewing_version == versions[0],
                  prev_version=prev_version, categories=categories)
        return super().get_context_data(**kw)


def _create_revision(scenario, description: str, user, expected_behavior: list | None = None, test_prompt: str = "",
                     severity_ceiling: str = "", documents=None, file_uri=None) -> ScenarioRevision:
    """Create the next revision for a scenario."""
    rev = scenario.revisions.count() + 1
    eb = expected_behavior or []
    docs = documents or []
    return ScenarioRevision.objects.create(
        scenario=scenario, revision=rev, description=description,
        expected_behavior=eb, test_prompt=test_prompt,
        severity_ceiling=severity_ceiling or "", documents=docs, file_uri=file_uri,
        content_hash=scenario_revision_hash(description=description, expected_behavior=eb, test_prompt=test_prompt,
                                            severity_ceiling=severity_ceiling or "", documents=docs, file_uri=file_uri, metadata={}),
        created_by=user,
    )


def _scenario_redirect(set_id: str | None):
    return redirect(f"/scenarios/?set={set_id}" if set_id else "/scenarios/")


def _publish_new_version(sset, user, extra_scenario_ids=None):
    """Auto-publish a new version capturing all current (non-archived) scenarios in the set."""
    latest = sset.versions.order_by("-version").first()
    if latest:
        scenario_ids = list(latest.items.values_list("scenario_id", flat=True))
    else:
        scenario_ids = []
    if extra_scenario_ids:
        for sid in extra_scenario_ids:
            if sid not in scenario_ids:
                scenario_ids.append(sid)
    # Exclude archived scenarios
    archived_ids = set(Scenario.objects.filter(id__in=scenario_ids, archived_at__isnull=False).values_list("id", flat=True))
    scenario_ids = [sid for sid in scenario_ids if sid not in archived_ids]
    if scenario_ids:
        publish_scenario_set_version(scenario_set=sset, user=user, scenario_ids=scenario_ids)


class ScenarioSetCreateView(ProjectMixin, View):
    def post(self, request):
        blocked = _require_write_access(request)
        if blocked:
            return blocked
        name = request.POST.get("name", "").strip()
        desc = request.POST.get("description", "").strip()
        if name:
            ScenarioSet.objects.create(
                project=request.project, name=name, description=desc, created_by=request.user,
            )
            messages.success(request, f"Scenario set '{name}' created.")
        return redirect("/scenarios/")


class ScenarioSetRenameView(ProjectMixin, View):
    def post(self, request, set_id):
        blocked = _require_write_access(request)
        if blocked:
            return blocked
        sset = ScenarioSet.objects.filter(pk=set_id, project=request.project).first()
        if sset:
            name = request.POST.get("name", "").strip()
            desc = request.POST.get("description", "").strip()
            if name:
                sset.name = name
            sset.description = desc
            sset.save()
            messages.success(request, "Scenario set updated.")
        return redirect(f"/scenarios/?set={set_id}")


class ScenarioSetDeleteView(ProjectMixin, View):
    def post(self, request, set_id):
        blocked = _require_write_access(request)
        if blocked:
            return blocked
        sset = ScenarioSet.objects.filter(pk=set_id, project=request.project).first()
        if sset:
            try:
                sset.delete()
                messages.success(request, "Scenario set deleted.")
            except Exception:  # noqa: BLE001 - any FK constraint violation means "in use"
                messages.error(request, "Cannot delete: this set is referenced by audit runs.")
        return redirect("/scenarios/")


class ScenarioCreateView(ProjectMixin, View):
    def post(self, request):
        blocked = _require_write_access(request)
        if blocked:
            return blocked
        name = request.POST.get("name", "").strip()
        category = request.POST.get("category", "").strip()
        desc = request.POST.get("description", "")
        expected_behavior_raw = request.POST.get("expected_behavior", "").strip()
        expected_behavior = [line.strip() for line in expected_behavior_raw.splitlines() if line.strip()] if expected_behavior_raw else []
        severity_ceiling = request.POST.get("severity_ceiling", "").strip().lower()
        documents_raw = request.POST.get("documents", "").strip()
        documents = json.loads(documents_raw) if documents_raw else []
        file_uri_raw = request.POST.get("file_uri", "").strip()
        file_uri = file_uri_raw or None
        set_id = request.POST.get("set_id", "").strip()
        if name:
            key = hashlib.sha256(name.encode()).hexdigest()[:12]
            scenario, _created = Scenario.objects.get_or_create(
                project=request.project, key=key,
                defaults={"title": name, "category": category},
            )
            _create_revision(scenario, desc, request.user, expected_behavior=expected_behavior,
                             severity_ceiling=severity_ceiling, documents=documents, file_uri=file_uri)
            # Auto-publish new version including this scenario
            if set_id:
                sset = ScenarioSet.objects.filter(pk=set_id, project=request.project).first()
                if sset:
                    _publish_new_version(sset, request.user, extra_scenario_ids=[scenario.id])
            messages.success(request, f"Scenario '{name}' added.")
        return _scenario_redirect(set_id or None)


class ScenarioEditView(ProjectMixin, View):
    def post(self, request, scenario_id):
        blocked = _require_write_access(request)
        if blocked:
            return blocked
        scenario = Scenario.objects.filter(pk=scenario_id, project=request.project).first()
        set_id = request.POST.get("set_id", "").strip()
        if scenario:
            title = request.POST.get("title", "").strip()
            category = request.POST.get("category", "").strip()
            desc = request.POST.get("description", "")
            expected_behavior_raw = request.POST.get("expected_behavior", "").strip()
            expected_behavior = [line.strip() for line in expected_behavior_raw.splitlines() if line.strip()] if expected_behavior_raw else []
            severity_ceiling = request.POST.get("severity_ceiling", "").strip().lower()
            documents_raw = request.POST.get("documents", "").strip()
            documents = json.loads(documents_raw) if documents_raw else []
            file_uri_raw = request.POST.get("file_uri", "").strip()
            file_uri = file_uri_raw or None
            if title:
                scenario.title = title
            scenario.category = category
            scenario.save()

            # Only create a new revision + publish if content actually changed
            latest_rev = scenario.revisions.order_by("-revision").first()
            content_changed = (
                latest_rev is None
                or latest_rev.description != desc
                or (latest_rev.expected_behavior or []) != expected_behavior
                or latest_rev.severity_ceiling != severity_ceiling
                or (latest_rev.documents or []) != documents
                or latest_rev.file_uri != file_uri
            )
            if content_changed:
                _create_revision(scenario, desc, request.user, expected_behavior=expected_behavior,
                                 severity_ceiling=severity_ceiling, documents=documents, file_uri=file_uri)
                if set_id:
                    sset = ScenarioSet.objects.filter(pk=set_id, project=request.project).first()
                    if sset:
                        _publish_new_version(sset, request.user)
                messages.success(request, f"Scenario '{scenario.title}' updated (new version published).")
            else:
                messages.info(request, f"Scenario '{scenario.title}' saved (no content change, version unchanged).")
        return _scenario_redirect(set_id or None)


class ScenarioDeleteView(ProjectMixin, View):
    def post(self, request, scenario_id):
        blocked = _require_write_access(request)
        if blocked:
            return blocked
        set_id = request.POST.get("set_id", "").strip()
        scenario = Scenario.objects.filter(pk=scenario_id, project=request.project).first()
        if scenario:
            # Archive instead of delete (preserves historical versions)
            scenario.archived_at = timezone.now()
            scenario.save()
            # Publish new version excluding archived scenarios
            if set_id:
                sset = ScenarioSet.objects.filter(pk=set_id, project=request.project).first()
                if sset:
                    _publish_new_version(sset, request.user)
        return _scenario_redirect(set_id or None)


class ScenarioRevertView(ProjectMixin, View):
    """Revert a scenario set to an old version by publishing it as a new version."""

    def get(self, request, set_id):
        """What reverting would change: the latest version -> ``target_version``."""
        from scenarios.services import version_diff

        sset = ScenarioSet.objects.filter(pk=set_id, project=request.project).first()
        if not sset:
            return JsonResponse({"error": "Not found"}, status=404)
        target = sset.versions.filter(version=_int(request.GET.get("target_version"))).first()
        latest = sset.versions.order_by("-version").first()
        if not target or not latest:
            return JsonResponse({"error": "Version not found"}, status=404)
        return JsonResponse(version_diff(latest, target))

    def post(self, request, set_id):
        blocked = _require_write_access(request)
        if blocked:
            return blocked
        sset = ScenarioSet.objects.filter(pk=set_id, project=request.project).first()
        if not sset:
            return redirect("/scenarios/")
        target_ver = _int(request.POST.get("target_version"))
        old_version = sset.versions.filter(version=target_ver).first()
        if old_version:
            scenario_ids = list(old_version.items.values_list("scenario_id", flat=True))
            if scenario_ids:
                publish_scenario_set_version(scenario_set=sset, user=request.user, scenario_ids=scenario_ids)
                messages.success(request, f"Reverted to v{target_ver} (published as new version).")
            else:
                messages.error(request, "Old version has no scenarios.")
        else:
            messages.error(request, "Version not found.")
        return redirect(f"/scenarios/?set={set_id}")


class ScenarioDiffView(ProjectMixin, View):
    """What changed between two versions of a scenario set (``?from=&to=``)."""

    def get(self, request, set_id):
        from scenarios.services import version_diff

        sset = ScenarioSet.objects.filter(pk=set_id, project=request.project).first()
        if not sset:
            return JsonResponse({"error": "Not found"}, status=404)
        old = sset.versions.filter(version=_int(request.GET.get("from"))).first()
        new = sset.versions.filter(version=_int(request.GET.get("to"))).first()
        if not old or not new:
            return JsonResponse({"error": "Version not found"}, status=404)
        return JsonResponse(version_diff(old, new))


class ScenarioExportView(ProjectMixin, View):
    def get(self, request, set_id):
        sset = ScenarioSet.objects.filter(pk=set_id, project=request.project).first()
        if not sset:
            return JsonResponse({"error": "Not found"}, status=404)
        latest = sset.versions.order_by("-version").first()
        def _export_scenario(it):
            d = {"key": it.scenario.key, "title": it.scenario.title,
                 "description": it.revision.description}
            if it.scenario.category:
                d["category"] = it.scenario.category
            if it.revision.expected_behavior:
                d["expected_behavior"] = it.revision.expected_behavior
            if it.revision.test_prompt:
                d["test_prompt"] = it.revision.test_prompt
            if it.revision.severity_ceiling:
                d["severity_ceiling"] = it.revision.severity_ceiling
            if it.revision.documents:
                d["documents"] = it.revision.documents
            if it.revision.file_uri:
                d["file_uri"] = it.revision.file_uri
            return d

        scenarios = [_export_scenario(it) for it in latest.items.select_related("scenario", "revision")] if latest else []
        return JsonResponse({"set_name": sset.name, "scenarios": scenarios})


class ScenarioImportView(ProjectMixin, View):
    def post(self, request, set_id):
        blocked = _require_write_access(request)
        if blocked:
            return blocked
        sset = ScenarioSet.objects.filter(pk=set_id, project=request.project).first()
        if not sset:
            return JsonResponse({"error": "Not found"}, status=404)
        try:
            data = json.loads(request.body)
            new_ids = []
            for item in data.get("scenarios", []):
                key = item.get("key", f"imported_{int(timezone.now().timestamp())}")
                scenario, _ = Scenario.objects.get_or_create(
                    project=request.project, key=key,
                    defaults={"title": item.get("title", "Imported"), "category": item.get("category", "")},
                )
                _create_revision(
                    scenario, item.get("description", ""), request.user,
                    expected_behavior=item.get("expected_behavior") or [],
                    test_prompt=item.get("test_prompt", ""),
                    severity_ceiling=item.get("severity_ceiling", ""),
                    documents=item.get("documents") or [],
                    file_uri=item.get("file_uri"),
                )
                new_ids.append(scenario.id)
            # Auto-publish after import (include newly imported scenarios)
            _publish_new_version(sset, request.user, extra_scenario_ids=new_ids)
        except Exception as e:  # noqa: BLE001 - surface any import/publish failure to the user
            return JsonResponse({"error": str(e)}, status=400)
        return redirect(f"/scenarios/?set={set_id}")


# ─── Models ──────────────────────────────────────────────────────────────────

DESCRIPTION_MAX = 1000


class ConnectionsView(ProjectMixin, TemplateView):
    """Connections (a server and its API key) and the models registered on each."""

    template_name = "connections.html"

    def get_context_data(self, **kw):
        from model_registry.models import ModelConnection
        from model_registry.services import (
            PROVIDER_PRESETS,
            admin_workspaces,
            can_edit_connection,
            connection_share_label,
            model_usage_counts,
            visible_connections_for,
        )

        p = self.request.project
        user = self.request.user
        # Owner connections first, then shared ones (see visible_connections_for).
        connections = visible_connections_for(user, p)
        # Attach each connection's models once (owner + shared alike).
        for conn in connections:
            conn.model_list = sorted(conn.models.all(), key=lambda m: (m.display_name or m.model_id).lower())
        usage = model_usage_counts(p)
        for conn in connections:
            for m in conn.model_list:
                m.usage = usage.get(m.id, 0)
            conn.in_use = any(m.usage for m in conn.model_list)
            conn.can_edit = can_edit_connection(user, conn)
            conn.share_label = connection_share_label(conn)
            conn.is_shared = not conn.is_owner
        # What the edit dialog needs (never the key itself). Only owner
        # connections are editable; shared ones render read-only.
        conn_data = {
            c.pk: {
                "name": c.name, "description": c.description, "provider": c.provider, "base_url": c.base_url, "secret_ref": c.secret_reference,
                "key_mode": "stored" if c.api_key_direct else "env" if c.secret_reference else "none",
                "enabled": c.enabled,
                "visibility": c.visibility,
                "shared_with": [str(x) for x in c.shared_with.values_list("id", flat=True)],
                "can_edit": c.can_edit,
                "share_label": c.share_label,
            }
            for c in connections
        }
        # Workspaces this user may share to (admins level / explicit picker).
        share_targets = [
            {"id": str(w.id), "name": w.name}
            for w in admin_workspaces(user)
            if w.id != p.id
        ]
        kw.update(
            connections=connections,
            conn_data=conn_data,
            model_total=sum(len(c.model_list) for c in connections),
            # The per-model "open in chat" icon only makes sense when the chat
            # module is on; otherwise /chat/ 404s.
            chat_enabled=chat_enabled(),
            provider_presets=PROVIDER_PRESETS,
            # Each provider once: presets share some (OpenAI and "Custom" are
            # both openai), and a connection's own provider must stay pickable.
            providers=list(dict.fromkeys([p[2] for p in PROVIDER_PRESETS] + [c.provider for c in connections if c.provider])),
            share_targets=share_targets,
            visibility_choices=[(v, label) for v, label in ModelConnection.Visibility.choices],
        )
        return super().get_context_data(**kw)

    def post(self, request, *args, **kwargs):
        from model_registry.models import ModelConnection, RegisteredModel

        blocked = _require_write_access(request)
        if blocked:
            return blocked
        p = request.project
        post = request.POST
        action = post.get("action")
        anchor = ""

        def fail(msg):
            messages.error(request, msg)
            return redirect(reverse("connections") + anchor)

        if action in ("add_connection", "edit_connection"):
            name = post.get("conn_name", "").strip()
            base_url = post.get("conn_base_url", "").strip()
            error = _base_url_error(base_url) or (None if name else "Connection name is required.")
            if action == "add_connection":
                conn = ModelConnection(project=p, created_by=request.user, enabled=True)
            else:
                # Editing is owner-workspace only: a shared connection's id must
                # belong to this workspace, otherwise it's read-only for us.
                conn = ModelConnection.objects.filter(pk=post.get("conn_id"), project=p).first()
                if conn is None:
                    return fail("Connection not found (or you can't edit a shared connection here).")
                conn.enabled = post.get("conn_enabled") == "1"
                anchor = f"#conn-{conn.pk}"
            if error:
                return fail(error)
            if ModelConnection.objects.filter(project=p, name=name).exclude(pk=conn.pk).exists():
                return fail(f"A connection named “{name}” already exists.")
            conn.name, conn.base_url = name, base_url
            conn.description = post.get("conn_description", "").strip()[:DESCRIPTION_MAX]
            conn.provider = post.get("conn_provider") or "openai"
            # Sharing level + explicit workspaces (owner-only; ignored for adds
            # that don't send them, defaulting to "this workspace only").
            visibility = post.get("conn_visibility", ModelConnection.Visibility.WORKSPACE)
            if visibility not in dict(ModelConnection.Visibility.choices):
                visibility = ModelConnection.Visibility.WORKSPACE
            conn.visibility = visibility
            if action == "edit_connection":
                shared_ids = [int(x) for x in post.getlist("conn_shared_with") if x.isdigit()]
                conn.shared_with.set(shared_ids)
            # Where the key comes from: stored on the connection, an env var, or none.
            key_mode = post.get("key_mode", "stored")
            if key_mode == "env":
                ref = post.get("conn_secret_ref", "").strip()
                if not ref:
                    return fail("Enter the environment variable that holds the API key.")
                conn.secret_reference, conn.api_key_direct = ref, ""
            elif key_mode == "none":
                conn.secret_reference, conn.api_key_direct = "", ""
            else:
                conn.secret_reference = ""
                new_key = post.get("conn_api_key", "").strip()
                if new_key:   # blank keeps the stored key
                    conn.api_key_direct = new_key
            conn.save()
            anchor = f"#conn-{conn.pk}"
            messages.success(request, f"Connection “{conn.name}” saved.")
        elif action == "add_models":
            # Adding models mutates the connection — owner workspace only.
            conn = ModelConnection.objects.filter(pk=post.get("conn_id"), project=p).first()
            if conn is None:
                return fail("Connection not found (or you can't edit a shared connection here).")
            anchor = f"#conn-{conn.pk}"
            ids = [m.strip() for m in post.getlist("model_id") if m.strip()][:200]
            if not ids:
                return fail("Enter a model ID or pick models to add.")
            single = len(ids) == 1
            label = post.get("model_display_name", "").strip() if single else ""
            desc = post.get("model_description", "").strip()[:DESCRIPTION_MAX] if single else ""
            existing = set(conn.models.values_list("model_id", flat=True))
            new = [RegisteredModel(connection=conn, project=p, model_id=m, display_name=label or m, description=desc, enabled=True)
                   for m in dict.fromkeys(ids) if m not in existing]
            RegisteredModel.objects.bulk_create(new)
            skipped = len(set(ids)) - len(new)
            messages.success(request, f"Added {len(new)} model{'s' if len(new) != 1 else ''} to {conn.name}."
                             + (f" {skipped} already there." if skipped else ""))
        elif action in ("edit_model", "delete_model"):
            # Editing/removing a model mutates its connection — owner workspace only.
            rm = (
                RegisteredModel.objects.select_related("connection")
                .filter(pk=post.get("rm_id"), project=p, connection__project=p)
                .first()
            )
            if rm is None:
                return fail("Model not found (or you can't edit a shared connection here).")
            anchor = f"#conn-{rm.connection_id}"
            if action == "edit_model":
                new_id = post.get("model_id_new", "").strip() or rm.model_id
                if new_id != rm.model_id and RegisteredModel.objects.filter(connection=rm.connection, model_id=new_id).exists():
                    return fail(f"“{new_id}” is already registered on {rm.connection.name}.")
                rm.model_id = new_id
                rm.display_name = post.get("model_display_name", "").strip() or new_id
                rm.description = post.get("model_description", "").strip()[:DESCRIPTION_MAX]
                rm.save(update_fields=["model_id", "display_name", "description"])
                messages.success(request, f"Saved {rm.display_name}.")
            else:
                try:
                    rm.delete()
                    messages.success(request, f"Removed {rm.display_name or rm.model_id}.")
                except (ProtectedError, RestrictedError):
                    # Runs pin models (RESTRICT FKs); deleting would break their records.
                    return fail(f"Can't delete “{rm.display_name}”: runs or monitors use it. Kept for reproducibility.")
        return redirect(reverse("connections") + anchor)


class ConnectionDeleteView(ProjectMixin, View):
    def post(self, request, conn_id):
        from model_registry.models import ModelConnection

        blocked = _require_write_access(request)
        if blocked:
            return blocked
        conn = ModelConnection.objects.filter(pk=conn_id, project=request.project).first()
        if conn:
            try:
                conn.delete()
                messages.success(request, f"Connection '{conn.name}' deleted.")
            except (ProtectedError, RestrictedError):
                # AuditRun.target_model / auditor_model / judge_model are RESTRICT FKs
                # to RegisteredModel; deleting a connection whose models are pinned by
                # audit runs would break the immutable experiment record. Django raises
                # RestrictedError for RESTRICT and ProtectedError for PROTECT.
                messages.error(
                    request,
                    "Cannot delete: this connection's models are referenced by audit runs or monitors.",
                )
        return redirect("connections")


def _base_url_error(raw: str) -> str | None:
    """Error message for an invalid connection base URL, or None when it is valid."""
    from django.core.exceptions import ValidationError

    from model_registry.serializers import EndpointURLField

    if not raw.strip():
        return "Base URL is required."
    try:
        EndpointURLField().to_internal_value(raw)
    except ValidationError:
        return "Base URL must be an http(s) URL, e.g. https://api.openai.com/v1."
    return None


class ConnectionCheckView(ProjectMixin, View):
    """Try the connection dialog's values before saving: list the server's
    models with that base URL, provider and key. Editing with the key left
    blank uses the connection's stored key."""

    def post(self, request):
        from model_registry.models import ModelConnection
        from model_registry.services import (
            can_edit_connection,
            fetch_remote_model_ids,
            http_error_detail,
        )

        blocked = _require_write_access(request)
        if blocked:
            return JsonResponse({"ok": False, "error": "You don't have permission to change connections."}, status=403)
        post = request.POST
        mode = post.get("key_mode", "stored")
        conn = ModelConnection(provider=(post.get("conn_provider") or "openai").strip(),
                               base_url=(post.get("conn_base_url") or "").strip())
        if mode == "stored":
            conn.api_key_direct = (post.get("conn_api_key") or "").strip()
            saved = ModelConnection.objects.filter(pk=_int(post.get("conn_id"))).first()
            if not conn.api_key_direct and saved is not None and can_edit_connection(request.user, saved):
                conn.api_key_direct = saved.api_key_direct
        elif mode == "env":
            conn.secret_reference = (post.get("conn_secret_ref") or "").strip()
        try:
            ids = fetch_remote_model_ids(conn)
        except Exception as e:  # noqa: BLE001 - any failure is the answer the user asked for
            return JsonResponse({"ok": False, "error": http_error_detail(e)})
        return JsonResponse({"ok": True, "count": len(ids), "models": ids[:6]})


class DiscoverModelsView(ProjectMixin, View):
    """List the models a connection's server offers (GET {base_url}/models)."""

    def post(self, request):
        from model_registry.models import ModelConnection
        from model_registry.services import fetch_remote_model_ids, http_error_detail

        # Looked up server-side (scoped to the workspace) so the API key never
        # reaches the browser.
        conn = ModelConnection.objects.filter(pk=request.POST.get("connection_id") or 0, project=request.project).first()
        if conn is None:
            return JsonResponse({"error": "Connection not found in this workspace."}, status=404)
        try:
            models = fetch_remote_model_ids(conn)
        except ValueError as e:
            return JsonResponse({"error": str(e)}, status=400)
        except Exception as e:  # noqa: BLE001 - surface any upstream failure to the user
            return JsonResponse({"error": http_error_detail(e)}, status=502)
        return JsonResponse({"models": models})


# ─── Compare ─────────────────────────────────────────────────────────────────

class CompareView(ProjectMixin, TemplateView):
    template_name = "compare.html"

    @staticmethod
    def _parse_run_ids(raw_value: str) -> list[int]:
        return [int(x) for x in (raw_value or "").split(",") if x.strip().isdigit()]

    @staticmethod
    def _reshape_result(raw: dict) -> dict:
        columns = [f"#{r['id']} ({r['target'] or '?'})" for r in raw["runs"]]
        rows = []
        for entry in raw["results"]:
            values = []
            for col_run_id in [str(r["id"]) for r in raw["runs"]]:
                rdata = entry["runs"].get(col_run_id, {})
                values.append(rdata.get("severity") or rdata.get("status") or "—")
            rows.append({"scenario": entry["scenario_key"], "values": values})
        # Per-run header metadata (one query for all runs)
        run_objs = AuditRun.objects.select_related("target_model").in_bulk([r["id"] for r in raw["runs"]])
        run_meta = []
        for r in raw["runs"]:
            run_obj = run_objs.get(r["id"])
            run_meta.append({
                "id": r["id"],
                "name": run_obj.name if run_obj else f"Run #{r['id']}",
                "target": frozen_name(run_obj, "target") if run_obj else (r["target"] or "?"),
            })
        return {
            "warnings": raw["warnings"],
            "columns": columns,
            "rows": rows,
            "inputs": raw.get("inputs", []),
            "run_meta": run_meta,
            "intersection_count": raw["intersection_count"],
        }

    def get_context_data(self, **kw):
        p = self.request.project
        selected_ids = self._parse_run_ids(self.request.GET.get("runs", ""))
        result = None
        error = None
        if selected_ids:
            if len(selected_ids) < 2:
                error = "Select at least 2 runs to compare."
            else:
                try:
                    result = self._reshape_result(compare_runs(p, selected_ids))
                except Exception as e:  # noqa: BLE001 - surface any comparison failure to the user
                    error = str(e)
        kw.update(
            runs=AuditRun.objects.filter(project=p, status="completed").select_related(
                "scenario_set_version__scenario_set"
            ).order_by("-created_at")[:50],
            selected_ids=selected_ids,
            result=result,
            error=error,
        )
        return super().get_context_data(**kw)

    def post(self, request, *args, **kwargs):
        ids = [int(x) for x in request.POST.getlist("runs[]") if x.isdigit()]
        result = None
        error = None
        if len(ids) < 2:
            error = "Select at least 2 runs to compare."
        else:
            try:
                result = self._reshape_result(compare_runs(request.project, ids))
            except Exception as e:  # noqa: BLE001 - surface any comparison failure to the user
                error = str(e)
        return self.render_to_response(self.get_context_data(result=result, error=error, selected_ids=ids))


# ─── Audit Detail ────────────────────────────────────────────────────────────

def _result_rows(run: AuditRun, items: dict | None = None) -> list[dict]:
    """Rows for the audit detail Results list (handles n_repetitions > 1)."""
    set_id = run.scenario_set_version.scenario_set_id
    if items is None:
        items = {
            str(vi.pk): vi
            for vi in ScenarioSetVersionItem.objects.filter(version=run.scenario_set_version).select_related("scenario")
        }
    n_reps = int((run.generation_parameters_snapshot or {}).get("n_repetitions") or 1)
    results = []
    for sr in ScenarioResult.objects.filter(run_id=run.id).order_by("id"):
        item = items.get(sr.version_item_id)
        r = sr.result or {}
        # When n_repetitions > 1, the result dict has "reps" + "aggregated_severity"
        summary = r.get("summary", "")
        if n_reps > 1 and "reps" in r:
            severity = r.get("aggregated_severity", sr.status)
            # Stored as a 0-1 fraction; the list shows a percentage.
            agreement = r.get("agreement_rate")
            agreement = agreement * 100 if agreement is not None else None
            sev_dist = r.get("severity_distribution", {})
            # Repeated results have no top-level summary: show the summary of
            # the first repetition that reached the aggregated verdict.
            reps = [rep for rep in r["reps"] if isinstance(rep, dict)]
            match = next((rep for rep in reps if rep.get("severity") == severity), reps[0] if reps else {})
            summary = summary or match.get("summary", "")
        else:
            severity = r.get("severity", sr.status)
            agreement = None
            sev_dist = None
        results.append({
            "result_id": sr.pk,
            "version_item_id": sr.version_item_id,
            "scenario_name": item.scenario.title if item else sr.version_item_id,
            "scenario_id": item.scenario_id if item else None,
            "set_id": set_id,
            "severity": severity,
            "summary": summary,
            "status": sr.status,
            "agreement_rate": agreement,
            "severity_distribution": sev_dist,
            "n_reps": n_reps if (n_reps > 1 and "reps" in r) else None,
        })
    return results


class RunResultsFragmentView(ProjectMixin, View):
    """GET /runs/<id>/results-fragment/ — the Results list alone, for live refresh."""

    def get(self, request, run_id):
        from django.template.loader import render_to_string

        run = get_object_or_404(
            AuditRun.objects.select_related("scenario_set_version"), pk=run_id, project=request.project
        )
        html = render_to_string(
            "partials/run_results.html", {"run": run, "results": _result_rows(run)}, request=request
        )
        return HttpResponse(html)


class RunDetailView(ProjectMixin, DetailView):
    template_name = "run_detail.html"
    context_object_name = "run"
    pk_url_kwarg = "run_id"
    queryset = AuditRun.objects.select_related(
        "scenario_set_version__scenario_set", "target_model", "auditor_model", "judge_model", "monitor", "experiment"
    )

    def get_queryset(self):
        return self.queryset.filter(project=self.request.project)

    def get_context_data(self, **kw):
        ctx = super().get_context_data(**kw)
        run = self.object
        set_id = run.scenario_set_version.scenario_set_id
        items = {str(vi.pk): vi for vi in ScenarioSetVersionItem.objects.filter(version=run.scenario_set_version).select_related("scenario")}
        results = _result_rows(run, items)
        ctx["results"] = results
        # Version item ids that already have a result row; the detail page seeds
        # its SSE dedupe set from this. Pre-serialized to a JSON array because
        # Django's template engine has no built-in `map`/`json` filters.
        ctx["result_version_item_ids_json"] = json.dumps(
            [r["version_item_id"] for r in results]
        )
        ctx["vi_names_json"] = json.dumps(
            {str(vi.pk): vi.scenario.title for vi in items.values()}
        )
        # Live progress starts from each scenario's latest event and streams
        # only newer ones, so page load stays cheap for big runs.
        if run.is_active:
            from audits.events import progress_snapshot

            snap_events, snap_last_id = progress_snapshot(run.id)
            ctx["progress_snapshot_json"] = json.dumps(snap_events)
            ctx["progress_last_event_id"] = snap_last_id
        ctx["set_id"] = set_id
        ctx["progress_pct"] = (run.completed_scenarios * 100 // run.total_scenarios) if run.total_scenarios else 0
        ctx["stages"] = ["queued", "preparing", "target_execution", "auditing", "judging", "aggregation", "completed"]
        ctx["title_suffix"] = f"(Run #{self.object.pk})"
        ctx["duration"] = run.duration_display
        from audits.monitors import has_write_role

        ctx["can_schedule"] = has_write_role(self.request.user, self.request.project)
        from audits.services import ROLES, frozen_judge, frozen_model

        ctx["frozen_models"] = [frozen_model(run, role) for role in ROLES]
        ctx["judge"] = frozen_judge(run)
        ctx["system_prompt"] = (run.generation_parameters_snapshot or {}).get("system_prompt", "")
        return ctx


class RunCancelView(ProjectMixin, View):
    def post(self, request, run_id):
        blocked = _require_write_access(request)
        if blocked:
            return blocked
        run = AuditRun.objects.filter(pk=run_id, project=request.project).first()
        if run:
            from audits.services import cancel_run

            cancel_run(run, request.user)
        return redirect(f"/runs/{run_id}/")


class RunArchiveView(ProjectMixin, View):
    """Toggle the soft-archive flag on a run. Project-scoped: runs outside
    the active project are invisible (404)."""

    def post(self, request, run_id):
        blocked = _require_write_access(request)
        if blocked:
            return blocked
        run = AuditRun.objects.filter(pk=run_id, project=request.project).first()
        if run:
            run.archived = not run.archived
            run.save(update_fields=["archived"])
        return redirect(request.META.get("HTTP_REFERER") or f"/runs/{run_id}/")


class ExperimentRenameView(ProjectMixin, View):
    """Rename an experiment (a display label; its runs are unchanged)."""

    def post(self, request, experiment_id):
        from audits.models import Experiment

        blocked = _require_write_access(request)
        if blocked:
            return blocked
        experiment = get_object_or_404(Experiment, pk=experiment_id, project=request.project)
        name = request.POST.get("name", "").strip()
        if name and len(name) <= 250:
            experiment.name = name
            experiment.save(update_fields=["name", "updated_at"])
            messages.success(request, "Experiment renamed.")
        else:
            messages.error(request, "Name must be 1-250 characters.")
        return redirect("experiment_detail", experiment_id)


class RunRenameView(ProjectMixin, View):
    """Rename an audit run. The name is a display label only; it does not
    affect the frozen reproducibility manifest."""

    def post(self, request, run_id):
        blocked = _require_write_access(request)
        if blocked:
            return blocked
        run = AuditRun.objects.filter(pk=run_id, project=request.project).first()
        if run:
            name = request.POST.get("name", "").strip()
            if name and len(name) <= 250:
                run.name = name
                run.save(update_fields=["name"])
                messages.success(request, "Audit renamed.")
            else:
                messages.error(request, "Name must be 1-250 characters.")
        return redirect(f"/runs/{run_id}/")


# ─── Scenario Result Detail ──────────────────────────────────────────────────

_REP_TOKEN_ROLES = ("target", "auditor", "judge")
# Keys rendered as named sections (or intentionally hidden) on the result page.
_REP_KNOWN_KEYS = {
    "conversation", "issues_found", "issues", "positive_behaviors", "recommendations", "summary",
    "severity", "rationale", "evidence", "judge_rationale", "judgment", "scenario_name",
    "scenario_description", "expected_behavior", "_rep_index", "_language", "error",
    *(f"{r}_{d}_tokens" for r in _REP_TOKEN_ROLES for d in ("input", "output")),
}


def _as_text_list(value) -> list[str]:
    """Normalise issues / behaviours / recommendations to a list of strings."""
    if not value:
        return []
    if not isinstance(value, list):
        value = [value]
    out = []
    for item in value:
        if isinstance(item, dict):
            out.append(item.get("description") or item.get("issue") or json.dumps(item, ensure_ascii=False))
        else:
            out.append(str(item))
    return out


# Judgment keys already shown as their own sections.
_JUDGMENT_SHOWN = {"severity", "summary", "issues_found", "positive_behaviors", "recommendations", "score", "abstained"}


def _judge_grade(judgment: dict) -> dict:
    """What a judgment adds beyond severity: score, abstained, and its other fields.

    Score judges (helpfulness, factuality, abstention, own score formats) give
    a 1-10 score with dimension scores; yes/no judges give the answer; the
    checklist gives per-item results.
    """
    judgment = judgment if isinstance(judgment, dict) else {}
    fields, notes = [], []
    for key, value in judgment.items():
        if key in _JUDGMENT_SHOWN or key.startswith("_") or value in (None, "", [], {}):
            continue
        label = key.replace("_", " ").capitalize()
        if isinstance(value, bool):
            fields.append((label, "yes" if value else "no"))
        elif isinstance(value, (int, float)):
            fields.append((label, f"{value:g}"))
        elif isinstance(value, str) and len(value) <= 80:
            fields.append((label, value))
        elif isinstance(value, str):
            notes.append((label, value))
        else:
            notes.append((label, json.dumps(value, indent=2, ensure_ascii=False)))
    score = judgment.get("score")
    return {
        "score": f"{score:g}" if isinstance(score, (int, float)) else None,
        "abstained": judgment.get("abstained") if isinstance(judgment.get("abstained"), bool) else None,
        "fields": fields,
        "notes": notes,
    }


def _rep_view(rep: dict, index: int) -> dict:
    """One judged conversation (a repetition, or the whole single-rep result)."""
    conversation = []
    turn = 0
    for msg in rep.get("conversation") or []:
        role = (msg or {}).get("role", "")
        # SimpleAudit drives the target with the auditor as "user".
        if role == "user":
            turn += 1
        conversation.append({
            "speaker": {"user": "Auditor", "assistant": "Target"}.get(role, role.title() or "Message"),
            "is_target": role == "assistant",
            "turn": turn,
            "content": (msg or {}).get("content", ""),
        })
    tokens = [
        {
            "role": r.title(),
            "input": rep.get(f"{r}_input_tokens"),
            "output": rep.get(f"{r}_output_tokens"),
        }
        for r in _REP_TOKEN_ROLES
        if rep.get(f"{r}_input_tokens") is not None or rep.get(f"{r}_output_tokens") is not None
    ]
    total_tokens = sum((t["input"] or 0) + (t["output"] or 0) for t in tokens)
    grade = _judge_grade(rep.get("judgment"))
    return {
        "index": index,
        "severity": rep.get("severity", ""),
        "summary": rep.get("summary", ""),
        "grade": grade,
        "has_grade": bool(grade["score"] or grade["abstained"] is not None or grade["fields"] or grade["notes"]),
        "conversation": conversation,
        "turns": turn,
        "issues": _as_text_list(rep.get("issues_found", rep.get("issues"))),
        "positives": _as_text_list(rep.get("positive_behaviors")),
        "recommendations": _as_text_list(rep.get("recommendations")),
        "rationale": rep.get("rationale") or rep.get("evidence") or rep.get("judge_rationale") or "",
        "tokens": tokens,
        "total_tokens": total_tokens,
        "other": {k: v for k, v in rep.items() if k not in _REP_KNOWN_KEYS},
    }


class RunResultView(ProjectMixin, TemplateView):
    template_name = "run_result.html"

    def get_context_data(self, **kw):
        run_id = self.kwargs["run_id"]
        result_id = self.kwargs["result_id"]
        run = get_object_or_404(AuditRun, pk=run_id, project=self.request.project)
        sr = get_object_or_404(ScenarioResult, pk=result_id, run_id=run_id)

        item = (
            ScenarioSetVersionItem.objects.filter(pk=sr.version_item_id)
            .select_related("scenario", "revision")
            .first()
        )
        scenario_name = item.scenario.title if item else f"Scenario {sr.version_item_id}"
        result_data = sr.result or {}

        is_repeated = isinstance(result_data.get("reps"), list)
        raw_reps = [r for r in result_data["reps"] if isinstance(r, dict)] if is_repeated else [result_data]
        reps = [_rep_view(r, i + 1) for i, r in enumerate(raw_reps)] if result_data else []

        if is_repeated:
            severity = result_data.get("aggregated_severity") or (reps[0]["severity"] if reps else sr.status)
            raw_agreement = result_data.get("agreement_rate")
            # Stored as a fraction (0.6667); shown as a percentage.
            agreement_rate = round(raw_agreement * 100, 1) if raw_agreement is not None else None
            distribution = result_data.get("severity_distribution") or {}
        else:
            severity = result_data.get("severity", sr.status)
            agreement_rate = None
            distribution = {}
        for rep in reps:
            rep["dissent"] = is_repeated and rep["severity"] != severity
        # Open on the first repetition that disagrees with the verdict, if any.
        initial_rep = next((r["index"] for r in reps if r["dissent"]), 1)

        revision = item.revision if item else None
        first = raw_reps[0] if raw_reps else {}
        expected = (revision.expected_behavior if revision else None) or first.get("expected_behavior") or []

        kw.update(
            run=run,
            sr=sr,
            scenario_name=scenario_name,
            scenario_description=(revision.description if revision else "") or first.get("scenario_description", ""),
            expected_behavior=_as_text_list(
                [e.get("criterion", e) if isinstance(e, dict) else e for e in expected]
            ),
            test_prompt=revision.test_prompt if revision else "",
            severity=severity,
            is_repeated=is_repeated,
            reps=reps,
            n_reps=len(reps),
            initial_rep=initial_rep,
            agreement_rate=agreement_rate,
            low_agreement=agreement_rate is not None and agreement_rate < 80,
            distribution=[
                {"severity": sev, "count": n, "pct": round(n * 100 / len(reps), 1) if reps else 0}
                for sev, n in distribution.items()
            ],
            error=result_data.get("error", "") if sr.status != "completed" else "",
            raw_json=json.dumps(result_data, indent=2, ensure_ascii=False) if result_data else "",
        )
        return super().get_context_data(**kw)


# ─── Export Views ────────────────────────────────────────────────────────────

class RunExportView(ProjectMixin, View):
    """Export audit results as JSON or CSV download."""

    def get(self, request, run_id):
        run = get_object_or_404(AuditRun, pk=run_id, project=request.project)
        fmt = request.GET.get("format", "json").lower()

        items = {str(vi.pk): vi for vi in ScenarioSetVersionItem.objects.filter(version=run.scenario_set_version).select_related("scenario")}
        rows = []
        for sr in ScenarioResult.objects.filter(run_id=run.id).order_by("id"):
            item = items.get(sr.version_item_id)
            r = sr.result or {}
            rows.append({
                "id": sr.id,
                "scenario_name": item.scenario.title if item else str(sr.version_item_id),
                "severity": r.get("severity", sr.status),
                "summary": r.get("summary", ""),
                "result": r,
            })

        filename = f"audit_{run.id}_results.{fmt}"

        if fmt == "csv":
            buf = io.StringIO()
            writer = csv.writer(buf)
            writer.writerow(["id", "scenario_name", "severity", "summary"])
            for row in rows:
                writer.writerow([row["id"], row["scenario_name"], row["severity"], row["summary"]])
            response = HttpResponse(buf.getvalue(), content_type="text/csv; charset=utf-8")
        else:
            payload = json.dumps(rows, indent=2, ensure_ascii=False)
            response = HttpResponse(payload, content_type="application/json; charset=utf-8")

        response["Content-Disposition"] = f'attachment; filename="{filename}"'
        return response


class RunScriptView(ProjectMixin, View):
    """A standalone Python script that re-runs this run with the plain
    ``simpleaudit`` library (frozen scenarios, models, judge, settings).

    Served as a file download by default, or as JSON (``?format=json``) for
    the run page's copy/download modal."""

    def get(self, request, run_id):
        run = get_object_or_404(AuditRun, pk=run_id, project=request.project)
        from infra.codegen import generate_run_script

        script = generate_run_script(run)
        if request.GET.get("format") == "json":
            return JsonResponse({"script": script})
        response = HttpResponse(script, content_type="text/x-python; charset=utf-8")
        safe_name = "".join(c if c.isalnum() or c in "-_." else "_" for c in run.name)[:60] or "run"
        response["Content-Disposition"] = f'attachment; filename="rerun_{safe_name}_{run.id}.py"'
        return response


class JudgeScriptView(ProjectMixin, View):
    """A standalone snippet that builds this judge from its spec.

    Served as a file download by default, or as JSON (``?format=json``) for the
    judge page's copy/download modal. ``?v=N`` targets a specific version."""

    def get(self, request, judge_id):
        from infra.codegen import generate_judge_script
        from judges.models import Judge

        judge = get_object_or_404(Judge, pk=judge_id, project=request.project)
        versions = list(judge.versions.order_by("-version"))
        wanted = request.GET.get("v", "")
        shown = next((v for v in versions if str(v.version) == wanted), versions[0] if versions else None)
        if shown is None:
            raise Http404("This judge has no versions yet.")
        script = generate_judge_script(judge.name, shown.content())
        if request.GET.get("format") == "json":
            return JsonResponse({"script": script})
        response = HttpResponse(script, content_type="text/x-python; charset=utf-8")
        safe_name = "".join(c if c.isalnum() or c in "-_." else "_" for c in judge.name)[:60] or "judge"
        response["Content-Disposition"] = f'attachment; filename="{safe_name}_judge.py"'
        return response
