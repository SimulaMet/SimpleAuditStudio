"""Server-rendered UI — Django CBVs + Forms + HTMX."""
import csv
import hashlib
import io
import itertools
import json
import logging
import os

from django.contrib import messages
from django.contrib.auth import login, logout
from django.contrib.auth.forms import AuthenticationForm
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db.models import ProtectedError, RestrictedError
from django.http import HttpResponse, HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.utils import timezone
from django.views.generic import DetailView, ListView, TemplateView, View

from accounts import workos_auth
from accounts.models import User
from audits.comparison import compare_runs
from audits.events import ScenarioResult
from audits.models import AuditRun
from audits.services import create_audit_run, submit_audit_run
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

        from accounts.models import ProjectMembership

        if not request.user.is_superuser and not ProjectMembership.objects.filter(
            user=request.user, role=ProjectMembership.Role.ADMIN
        ).exists():
            from django.http import HttpResponseForbidden

            return HttpResponseForbidden("Admin access required.")
        return super().dispatch(request, *args, **kwargs)


class SuperuserRequiredMixin(LoginRequiredMixin):
    """Restrict a view to superusers only."""

    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return super().dispatch(request, *args, **kwargs)
        if not request.user.is_superuser:
            from django.http import HttpResponseForbidden

            return HttpResponseForbidden("Super admin access required.")
        return super().dispatch(request, *args, **kwargs)


def _require_writable_project(request):
    """Block mutations on an archived workspace for non-superusers (UI forms).

    Returns a redirect response with an error message when the active project
    is archived and the user is not a superuser; otherwise returns None so the
    view proceeds. (Raising the DRF StableAPIError here would 500 in a plain
    Django view, so we use the messages framework instead.)
    """
    project = getattr(request, "project", None)
    if project is not None and project.archived and not request.user.is_superuser:
        messages.error(request, "This workspace is archived and read-only.")
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

class IndexView(TemplateView):
    """Redirect helper kept for backward compatibility with the ``index`` name.

    ``/`` is the home page (dashboard when signed in, landing page otherwise;
    see ``config/urls.py``). This view simply forwards there.
    """

    def get(self, request, *args, **kwargs):
        return redirect("/")


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
            login(request, user)
            return redirect("dashboard")
        return self.render_to_response(self.get_context_data(error=error))


def logout_view(request):
    logout(request)
    return redirect("login")


def auto_login_view(request):
    """One-click sign-in for the local one-liner demo (`uvx simpleaudit-studio`).

    The CLI opens this URL in the default browser after startup; it logs the
    visitor in as the shared bootstrap user and lands them on the dashboard.
    Only enabled in MINIMAL_CONFIG (local demo) mode — 404 everywhere else.
    """
    from django.conf import settings
    from django.http import Http404

    if not getattr(settings, "MINIMAL_CONFIG", False):
        raise Http404
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


class WorkOSCallbackView(View):
    """Handle the WorkOS hosted AuthKit redirect (OAuth code flow).

    Kept for when the hosted UI is available (production environments).
    The primary flow uses the two-step Magic Auth above.
    """

    def get(self, request):
        from django.conf import settings

        if not settings.WORKOS_ENABLED:
            return redirect("login")
        error = request.GET.get("error")
        if error:
            messages.error(request, f"WorkOS sign-in failed: {request.GET.get('error_description', error)}")
            return redirect("login")

        expected_state = request.session.pop("workos_state", None)
        if not expected_state or request.GET.get("state") != expected_state:
            messages.error(request, "WorkOS sign-in failed: state mismatch. Please try again.")
            return redirect("login")

        code = request.GET.get("code", "")
        if not code:
            messages.error(request, "WorkOS sign-in failed: missing authorization code.")
            return redirect("login")

        try:
            user, created = workos_auth.exchange_code_for_user(
                code,
                ip_address=request.META.get("REMOTE_ADDR"),
                user_agent=request.META.get("HTTP_USER_AGENT", ""),
            )
        except Exception as exc:
            logger.exception("WorkOS code exchange failed")
            messages.error(request, f"WorkOS sign-in failed: {exc}")
            return redirect("login")

        login(request, user)
        if created:
            _grant_default_project(user)
            messages.success(request, "Welcome! Your account was created via WorkOS sign-in.")
        else:
            messages.success(request, "Signed in with WorkOS.")
        return redirect("dashboard")


def _grant_default_project(user):
    """Give first-time WorkOS users membership in the 'Default' project (viewer).

    The 'Default' workspace is reserved for this purpose — it is created during
    platform bootstrap and serves as the shared landing space for new users.
    """
    from accounts.models import Project, ProjectMembership

    project = Project.objects.filter(slug="default").first()
    if project:
        ProjectMembership.objects.get_or_create(
            project=project, user=user, defaults={"role": ProjectMembership.Role.VIEWER}
        )


# ─── Dashboard ───────────────────────────────────────────────────────────────

class DashboardView(ProjectMixin, ListView):
    template_name = "dashboard.html"
    context_object_name = "runs"
    paginate_by = 25

    _SORT_WHITELIST = {"created_at", "-created_at", "status", "-id"}

    def get_queryset(self):
        from django.db.models import Q

        qs = AuditRun.objects.filter(project=self.request.project).select_related(
            "scenario_set_version__scenario_set", "target_model", "auditor_model", "judge_model"
        )
        # Status filter via ?status=active|completed|failed|cancelled|archived
        status = self.request.GET.get("status", "")
        if status == "archived":
            qs = qs.filter(archived=True)
        else:
            qs = qs.filter(archived=False)
            if status == "active":
                qs = qs.exclude(status__in=["completed", "failed", "cancelled"])
            elif status in ("completed", "failed", "cancelled"):
                qs = qs.filter(status=status)
        # Search via ?q=
        q = (self.request.GET.get("q") or "").strip()
        if q:
            qs = qs.filter(Q(name__icontains=q) | Q(scenario_set_version__scenario_set__name__icontains=q))
        # Sort via ?sort= (whitelisted)
        sort = self.request.GET.get("sort", "-created_at")
        if sort not in self._SORT_WHITELIST:
            sort = "-created_at"
        qs = qs.order_by(sort)
        return qs

    def get_context_data(self, **kw):
        ctx = super().get_context_data(**kw)
        base = AuditRun.objects.filter(project=self.request.project)
        visible = base.filter(archived=False)
        ctx["stats"] = {
            "total": visible.count(),
            "active": visible.exclude(status__in=["completed", "failed", "cancelled"]).count(),
            "completed": visible.filter(status="completed").count(),
            "failed": visible.filter(status="failed").count(),
            "archived": base.filter(archived=True).count(),
        }
        ctx["current_status"] = self.request.GET.get("status", "")
        ctx["search_query"] = (self.request.GET.get("q") or "").strip()
        ctx["current_sort"] = self.request.GET.get("sort", "-created_at")
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


def _clone_from_run(source: AuditRun) -> dict:
    """Prefill for the shared audit form blocks from an existing run."""
    params = source.generation_parameters_snapshot or {}
    # Strip form-managed keys from the JSON so they don't appear in the Advanced
    # textarea (they're pre-filled in their own fields).
    _FORM_KEYS = {"max_turns", "n_repetitions", "language"}
    gen_params = {k: v for k, v in params.items() if k not in _FORM_KEYS}
    return {
        "scenario_set_id": source.scenario_set_version.scenario_set_id,
        "scenario_set_version_id": source.scenario_set_version_id,
        "target_model_id": source.target_model_id,
        "auditor_model_id": source.auditor_model_id,
        "judge_model_id": source.judge_model_id,
        "max_turns": params.get("max_turns", ""),
        "language": params.get("language", ""),
        "n_repetitions": params.get("n_repetitions", ""),
        "generation_json": json.dumps(gen_params, indent=2, sort_keys=True) if gen_params else "",
    }


class NewAuditView(ProjectMixin, TemplateView):
    template_name = "new_audit.html"

    def get_context_data(self, **kw):
        p = self.request.project
        source = _run_from_query(self.request, "clone_from")
        clone = _clone_from_run(source) if source else None
        from model_registry.models import ModelConnection

        connections = (
            ModelConnection.objects.filter(project=p)
            .prefetch_related("models")
            .order_by("name")
        )
        from audits.scheduling import has_write_role

        kw.setdefault("error", None)
        kw.update(
            sets=ScenarioSet.objects.filter(project=p),
            connections=connections,
            clone=clone,
            # Same rule as launching a run: admin or auditor (superusers too).
            can_launch=has_write_role(self.request.user, p),
        )
        return super().get_context_data(**kw)

    def post(self, request, *args, **kwargs):
        p = request.project
        blocked = _require_writable_project(request)
        if blocked:
            return blocked
        try:
            clone_version_id = (request.POST.get("scenario_set_version") or "").strip()
            if clone_version_id:
                version = ScenarioSetVersion.objects.get(id=clone_version_id, scenario_set__project=p)
            else:
                sset = ScenarioSet.objects.get(pk=request.POST["scenario_set"], project=p)
                version = sset.versions.order_by("-version").first()
                if not version:
                    raise ValueError("No published version for this set.")

            # Parse optional hyperparameter overrides
            max_turns_raw = (request.POST.get("max_turns") or "").strip()
            max_turns_override = int(max_turns_raw) if max_turns_raw else None
            language_override = (request.POST.get("language") or "").strip() or None
            n_reps_raw = (request.POST.get("n_repetitions") or "").strip()
            n_repetitions_override = int(n_reps_raw) if n_reps_raw and int(n_reps_raw) > 1 else None

            # Parse optional generation config JSON override
            gen_config_override = None
            gen_json_raw = (request.POST.get("gen_config_json") or "").strip()
            if gen_json_raw and gen_json_raw != "{}":
                try:
                    parsed = json.loads(gen_json_raw)
                    if not isinstance(parsed, dict):
                        raise TypeError("Must be a JSON object")
                    gen_config_override = parsed or None
                except Exception as e:  # noqa: BLE001 - any parse failure is a user error
                    return self.render_to_response(self.get_context_data(error=f"Invalid generation config JSON: {e}"))

            from model_registry.models import RegisteredModel

            run = create_audit_run(
                project=p,
                user=request.user,
                name=f"Audit {timezone.now():%Y-%m-%d %H:%M}",
                scenario_set_version=version,
                target_model=RegisteredModel.objects.get(pk=request.POST["target_model"], project=p),
                auditor_model=RegisteredModel.objects.get(pk=request.POST["auditor_model"], project=p),
                judge_model=RegisteredModel.objects.get(pk=request.POST["judge_model"], project=p),
                max_turns_override=max_turns_override,
                language_override=language_override,
                n_repetitions_override=n_repetitions_override,
                gen_config_override=gen_config_override,
            )
            submit_audit_run(run)
            return redirect(f"/audits/{run.id}/")
        except Exception as e:  # noqa: BLE001 - surface any creation failure to the user
            return self.render_to_response(self.get_context_data(error=str(e)))


# ─── Schedules (recurring audits / drift) ────────────────────────────────────

_INTERVAL_PRESETS = [(6, "Every 6 hours"), (24, "Daily"), (72, "Every 3 days"), (168, "Weekly"), (336, "Every 2 weeks")]


class SchedulesView(ProjectMixin, TemplateView):
    template_name = "schedules.html"

    def get_context_data(self, **kw):
        from audits.models import AuditSchedule
        from audits.scheduling import (
            MAX_SCHEDULES_PER_PROJECT,
            can_manage_schedule,
            has_write_role,
        )
        from model_registry.models import ModelConnection

        p = self.request.project
        schedules = list(
            AuditSchedule.objects.filter(project=p).select_related(
                "project", "scenario_set", "scenario_set_version", "target_model", "auditor_model", "judge_model",
                "last_run", "created_by",
            )
        )
        for s in schedules:
            s.can_manage = can_manage_schedule(self.request.user, s)
        kw.setdefault("error", None)
        # "Repeat on schedule" from an audit detail page: prefill from that run,
        # pinned to its exact scenario set version, so the run can be the first
        # point of the series.
        from_run = kw.get("from_run") or _run_from_query(self.request, "from_run")
        if from_run:
            kw.setdefault("clone", _clone_from_run(from_run))
            kw.setdefault("form", {
                "name": f"{from_run.target_model.display_name} · {from_run.scenario_set_version.scenario_set.name} drift",
            })
        kw["from_run"] = from_run
        kw.setdefault("form", {})
        # Prefill for the shared model / hyperparameter blocks (same shape as
        # NewAuditView's clone). Drift series default to 3 repetitions so the
        # confidence band is usable.
        kw.setdefault("clone", {"n_repetitions": 3})
        kw.update(
            schedules=schedules,
            sets=ScenarioSet.objects.filter(project=p).prefetch_related("versions"),
            connections=ModelConnection.objects.filter(project=p).prefetch_related("models").order_by("name"),
            interval_presets=_INTERVAL_PRESETS,
            timezones=_timezone_choices(),
            can_create=has_write_role(self.request.user, p) and not (p.archived and not self.request.user.is_superuser),
            at_cap=len(schedules) >= MAX_SCHEDULES_PER_PROJECT,
            max_schedules=MAX_SCHEDULES_PER_PROJECT,
        )
        return super().get_context_data(**kw)

    def post(self, request, *args, **kwargs):
        from datetime import datetime, timedelta

        from audits.models import AuditSchedule
        from audits.scheduling import (
            MAX_INTERVAL_HOURS,
            MAX_SCHEDULES_PER_PROJECT,
            MIN_INTERVAL_HOURS,
            cron_next,
            has_write_role,
            validate_cron,
            zone,
        )
        from audits.services import _generation_parameters
        from model_registry.models import RegisteredModel

        p = request.project
        blocked = _require_writable_project(request)
        if blocked:
            return blocked
        if not has_write_role(request.user, p):
            return HttpResponseForbidden("Admin or auditor role required to create schedules.")
        form = request.POST
        try:
            if AuditSchedule.objects.filter(project=p).count() >= MAX_SCHEDULES_PER_PROJECT:
                raise ValueError(
                    f"This workspace already has {MAX_SCHEDULES_PER_PROJECT} schedules (the limit). Delete one first."
                )
            name = (form.get("name") or "").strip()
            if not name:
                raise ValueError("Name is required.")
            sset = ScenarioSet.objects.get(pk=form["scenario_set"], project=p)
            pinned = None
            version_id = (form.get("scenario_set_version") or "").strip()
            if version_id:
                # Pinned to a specific version (e.g. the source run's).
                pinned = sset.versions.filter(pk=version_id).first()
                if pinned is None:
                    raise ValueError("That scenario set version does not belong to the selected set.")
            elif form.get("pin_version"):
                pinned = sset.versions.order_by("-version").first()
                if pinned is None:
                    raise ValueError("This scenario set has no published version to pin.")
            tz_name = (form.get("timezone") or "UTC").strip()
            tz = zone(tz_name)
            cron_expression = ""
            if form.get("interval_hours") == "cron":
                cron_expression = validate_cron(form.get("cron_expression") or "", tz=tz_name)
                interval_hours = 0
            else:
                interval_hours = int(form.get("interval_hours") or 168)
                if not MIN_INTERVAL_HOURS <= interval_hours <= MAX_INTERVAL_HOURS:
                    raise ValueError(f"Interval must be between {MIN_INTERVAL_HOURS} hours and 90 days.")
            first_raw = (form.get("first_run_at") or "").strip()
            if first_raw:
                # Wall-clock time in the schedule's timezone.
                first = timezone.make_aware(datetime.fromisoformat(first_raw), tz)
            elif not cron_expression:
                # A minute out: the next sweeper pass picks it up.
                first = timezone.now() + timedelta(minutes=1)
            if cron_expression:
                # First firing at or after the requested start (or now).
                start = first if first_raw else timezone.now()
                first = cron_next(cron_expression, start - timedelta(minutes=1), tz_name)

            max_turns_raw = (form.get("max_turns") or "").strip()
            n_reps_raw = (form.get("n_repetitions") or "").strip()
            n_reps = int(n_reps_raw) if n_reps_raw else None
            gen_config = None
            gen_json_raw = (form.get("gen_config_json") or "").strip()
            if gen_json_raw and gen_json_raw != "{}":
                gen_config = json.loads(gen_json_raw)
                if not isinstance(gen_config, dict):
                    raise ValueError("Generation config must be a JSON object.")

            models_ = {
                role: RegisteredModel.objects.get(pk=form[f"{role}_model"], project=p)
                for role in ("target", "auditor", "judge")
            }
            schedule = AuditSchedule.objects.create(
                project=p,
                name=name,
                scenario_set=sset,
                scenario_set_version=pinned,
                target_model=models_["target"],
                auditor_model=models_["auditor"],
                judge_model=models_["judge"],
                generation_parameters=_generation_parameters(
                    max_turns_override=int(max_turns_raw) if max_turns_raw else None,
                    language_override=(form.get("language") or "").strip() or None,
                    n_repetitions_override=n_reps if n_reps and n_reps > 1 else None,
                    gen_config_override=gen_config or None,
                ),
                interval_hours=interval_hours,
                cron_expression=cron_expression,
                timezone=tz_name,
                next_run_at=first,
                created_by=request.user,
            )
            messages.success(
                request, f"Schedule '{name}' created. First run {first.astimezone(tz):%Y-%m-%d %H:%M} {tz_name}."
            )
            baseline = _baseline_run(p, form)
            if baseline and form.get("attach_baseline"):
                if _matches_schedule(baseline, schedule):
                    baseline.schedule = schedule
                    baseline.save(update_fields=["schedule"])
                    schedule.last_run = baseline
                    schedule.save(update_fields=["last_run"])
                    messages.info(request, f"Run #{baseline.id} is the first point of the drift series.")
                else:
                    messages.info(
                        request,
                        f"Run #{baseline.id} was not added to the series: its settings differ from the schedule's.",
                    )
            return redirect(f"/schedules/{schedule.id}/")
        except Exception as e:  # noqa: BLE001 - surface any validation failure to the user
            return self.render_to_response(
                self.get_context_data(
                    error=str(e), form=form, clone=_clone_from_post(form), from_run=_baseline_run(p, form)
                )
            )


def _baseline_run(project, form):
    raw = (form.get("baseline_run") or "").strip()
    if not raw.isdigit():
        return None
    return (
        AuditRun.objects.filter(pk=int(raw), project=project, schedule__isnull=True)
        .select_related("scenario_set_version__scenario_set", "target_model", "auditor_model", "judge_model")
        .first()
    )


def _matches_schedule(run: AuditRun, schedule) -> bool:
    """Whether ``run`` is the same experiment the schedule will repeat."""
    from audits.scheduling import resolve_version

    version = resolve_version(schedule)
    return (
        version is not None
        and run.scenario_set_version_id == version.id
        and run.target_model_id == schedule.target_model_id
        and run.auditor_model_id == schedule.auditor_model_id
        and run.judge_model_id == schedule.judge_model_id
        and (run.generation_parameters_snapshot or {}) == (schedule.generation_parameters or {})
    )


def _timezone_choices() -> list[str]:
    """IANA zone names for the schedule form (UTC first, then alphabetical)."""
    from zoneinfo import available_timezones

    zones = sorted(z for z in available_timezones() if "/" in z and not z.startswith(("Etc/", "SystemV/")))
    return ["UTC", *zones]


def _clone_from_post(form) -> dict:
    """Re-fill the shared audit form blocks from a rejected POST."""

    def as_int(key):
        raw = (form.get(key) or "").strip()
        return int(raw) if raw.isdigit() else None

    return {
        "scenario_set_id": as_int("scenario_set"),
        "scenario_set_version_id": as_int("scenario_set_version"),
        "target_model_id": as_int("target_model"),
        "auditor_model_id": as_int("auditor_model"),
        "judge_model_id": as_int("judge_model"),
        "max_turns": form.get("max_turns", ""),
        "language": form.get("language", ""),
        "n_repetitions": form.get("n_repetitions", ""),
        "generation_json": form.get("gen_config_json", ""),
    }


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


class ScheduleDetailView(ProjectMixin, TemplateView):
    template_name = "schedule_detail.html"

    def get_context_data(self, **kw):
        from audits.models import AuditSchedule
        from audits.scheduling import BASELINE_WINDOW, can_manage_schedule, drift_series

        schedule = get_object_or_404(
            AuditSchedule.objects.select_related(
                "scenario_set", "scenario_set_version", "target_model", "auditor_model", "judge_model"
            ),
            pk=kw["schedule_id"],
            project=self.request.project,
        )
        points = drift_series(schedule)
        for prev, cur in itertools.pairwise(points):
            cur["prev_run_id"] = prev["run"].id
        kw.update(
            schedule=schedule,
            points=list(reversed(points)),
            chart=_drift_chart(points),
            baseline_window=BASELINE_WINDOW,
            can_manage=can_manage_schedule(self.request.user, schedule),
            drops=sum(1 for p in points if p["change"] == "drop"),
        )
        return super().get_context_data(**kw)


class ScheduleActionView(ProjectMixin, View):
    """POST /schedules/<id>/<action>/ — toggle | run-now | delete."""

    def post(self, request, schedule_id, action):
        from audits.models import AuditSchedule
        from audits.scheduling import (
            can_manage_schedule,
            launch_schedule,
            owner_authorized,
        )

        blocked = _require_writable_project(request)
        if blocked:
            return blocked
        schedule = get_object_or_404(
            AuditSchedule.objects.select_related("project", "created_by"), pk=schedule_id, project=request.project
        )
        if not can_manage_schedule(request.user, schedule):
            return HttpResponseForbidden("Only a workspace admin or the schedule's creator can do this.")
        if action == "toggle" and not schedule.enabled and not owner_authorized(schedule):
            messages.error(
                request,
                "Cannot resume: the schedule's creator no longer has admin or auditor role. Recreate it under your account.",
            )
        elif action == "toggle":
            schedule.enabled = not schedule.enabled
            if schedule.enabled and schedule.next_run_at < timezone.now():
                from audits.scheduling import next_after

                schedule.next_run_at = next_after(schedule, timezone.now())
            schedule.save(update_fields=["enabled", "next_run_at", "updated_at"])
            messages.success(request, f"Schedule '{schedule.name}' {'resumed' if schedule.enabled else 'paused'}.")
        elif action == "run-now":
            last = schedule.last_run
            if last and last.status not in (AuditRun.Status.COMPLETED, AuditRun.Status.FAILED, AuditRun.Status.CANCELLED):
                messages.error(request, f"Run #{last.id} from this schedule is still {last.status}.")
            else:
                try:
                    run = launch_schedule(schedule)
                except Exception as e:  # noqa: BLE001 - surface creation failure to the user
                    messages.error(request, f"Could not launch: {e}")
                else:
                    schedule.last_run = run
                    schedule.last_error = ""
                    schedule.save(update_fields=["last_run", "last_error", "updated_at"])
                    submit_audit_run(run)
                    return redirect(f"/audits/{run.id}/")
        elif action == "delete":
            name = schedule.name
            schedule.delete()
            messages.success(request, f"Schedule '{name}' deleted. Its runs are kept.")
            return redirect("/schedules/")
        return redirect(request.META.get("HTTP_REFERER") or f"/schedules/{schedule_id}/")


# ─── Queue ───────────────────────────────────────────────────────────────────

# ─── Scenarios ───────────────────────────────────────────────────────────────

class ScenariosView(ProjectMixin, TemplateView):
    template_name = "scenarios.html"

    def get_context_data(self, **kw):
        p = self.request.project
        sets = ScenarioSet.objects.filter(project=p).prefetch_related("versions__items__scenario").order_by("name")
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
        kw.update(sets=sets, selected=selected, items=items, versions=versions, viewing_version=viewing_version, prev_version=prev_version, categories=categories)
        return super().get_context_data(**kw)


def _content_hash(description: str, expected_behavior: list | None = None, test_prompt: str = "") -> str:
    payload = json.dumps({
        "description": description,
        "expected_behavior": expected_behavior or [],
        "test_prompt": test_prompt,
    }, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode()).hexdigest()


def _create_revision(scenario, description: str, user, expected_behavior: list | None = None, test_prompt: str = "") -> ScenarioRevision:
    """Create the next revision for a scenario."""
    rev = scenario.revisions.count() + 1
    eb = expected_behavior or []
    return ScenarioRevision.objects.create(
        scenario=scenario, revision=rev, description=description,
        expected_behavior=eb, test_prompt=test_prompt,
        content_hash=_content_hash(description, eb, test_prompt),
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
        blocked = _require_writable_project(request)
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
        blocked = _require_writable_project(request)
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
        blocked = _require_writable_project(request)
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
        blocked = _require_writable_project(request)
        if blocked:
            return blocked
        name = request.POST.get("name", "").strip()
        category = request.POST.get("category", "").strip()
        desc = request.POST.get("description", "")
        expected_behavior_raw = request.POST.get("expected_behavior", "").strip()
        expected_behavior = [line.strip() for line in expected_behavior_raw.splitlines() if line.strip()] if expected_behavior_raw else []
        set_id = request.POST.get("set_id", "").strip()
        if name:
            key = hashlib.sha256(name.encode()).hexdigest()[:12]
            scenario, _created = Scenario.objects.get_or_create(
                project=request.project, key=key,
                defaults={"title": name, "category": category},
            )
            _create_revision(scenario, desc, request.user, expected_behavior=expected_behavior)
            # Auto-publish new version including this scenario
            if set_id:
                sset = ScenarioSet.objects.filter(pk=set_id, project=request.project).first()
                if sset:
                    _publish_new_version(sset, request.user, extra_scenario_ids=[scenario.id])
            messages.success(request, f"Scenario '{name}' added.")
        return _scenario_redirect(set_id or None)


class ScenarioEditView(ProjectMixin, View):
    def post(self, request, scenario_id):
        blocked = _require_writable_project(request)
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
            )
            if content_changed:
                _create_revision(scenario, desc, request.user, expected_behavior=expected_behavior)
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
        blocked = _require_writable_project(request)
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
        """Return diff between current latest and target version."""
        sset = ScenarioSet.objects.filter(pk=set_id, project=request.project).first()
        if not sset:
            return JsonResponse({"error": "Not found"}, status=404)
        target_ver = int(request.GET.get("target_version", 0))
        target_version = sset.versions.filter(version=target_ver).first()
        latest_version = sset.versions.order_by("-version").first()
        if not target_version or not latest_version:
            return JsonResponse({"error": "Version not found"}, status=404)

        # Build maps: scenario_key -> {title, description, expected_behavior}
        def _snap(ver):
            m = {}
            for it in ver.items.select_related("scenario", "revision"):
                m[it.scenario.key] = {
                    "title": it.scenario.title,
                    "description": it.revision.description,
                    "expected_behavior": it.revision.expected_behavior or [],
                }
            return m

        latest_map = _snap(latest_version)
        target_map = _snap(target_version)

        added = []      # in target but not in latest
        removed = []    # in latest but not in target
        changed = []    # in both but content differs
        unchanged = []  # in both, same content

        all_keys = set(latest_map.keys()) | set(target_map.keys())
        for key in sorted(all_keys):
            in_latest = key in latest_map
            in_target = key in target_map
            if in_target and not in_latest:
                added.append({"key": key, **target_map[key]})
            elif in_latest and not in_target:
                removed.append({"key": key, **latest_map[key]})
            else:
                latest, target = latest_map[key], target_map[key]
                if latest["description"] != target["description"] or latest["expected_behavior"] != target["expected_behavior"]:
                    changed.append({"key": key, "title": target["title"],
                                    "latest_desc": latest["description"], "target_desc": target["description"],
                                    "latest_eb": latest["expected_behavior"], "target_eb": target["expected_behavior"]})
                else:
                    unchanged.append({"key": key, "title": target["title"]})

        return JsonResponse({
            "target_version": target_ver,
            "latest_version": latest_version.version,
            "added": added, "removed": removed, "changed": changed,
            "unchanged_count": len(unchanged),
        })

    def post(self, request, set_id):
        blocked = _require_writable_project(request)
        if blocked:
            return blocked
        sset = ScenarioSet.objects.filter(pk=set_id, project=request.project).first()
        if not sset:
            return redirect("/scenarios/")
        target_ver = int(request.POST.get("target_version", 0))
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
    """Return diff between any two versions of a scenario set."""

    def get(self, request, set_id):
        sset = ScenarioSet.objects.filter(pk=set_id, project=request.project).first()
        if not sset:
            return JsonResponse({"error": "Not found"}, status=404)
        from_ver = int(request.GET.get("from", 0))
        to_ver = int(request.GET.get("to", 0))
        ver_from = sset.versions.filter(version=from_ver).first()
        ver_to = sset.versions.filter(version=to_ver).first()
        if not ver_from or not ver_to:
            return JsonResponse({"error": "Version not found"}, status=404)

        def _snap(ver):
            m = {}
            for it in ver.items.select_related("scenario", "revision"):
                m[it.scenario.key] = {
                    "title": it.scenario.title,
                    "description": it.revision.description,
                    "expected_behavior": it.revision.expected_behavior or [],
                }
            return m

        from_map = _snap(ver_from)
        to_map = _snap(ver_to)

        added = []    # in 'to' but not in 'from'
        removed = []  # in 'from' but not in 'to'
        changed = []  # in both but content differs
        unchanged_count = 0

        all_keys = set(from_map.keys()) | set(to_map.keys())
        for key in sorted(all_keys):
            in_from = key in from_map
            in_to = key in to_map
            if in_to and not in_from:
                added.append({"key": key, **to_map[key]})
            elif in_from and not in_to:
                removed.append({"key": key, **from_map[key]})
            else:
                f, t = from_map[key], to_map[key]
                if f["description"] != t["description"] or f["expected_behavior"] != t["expected_behavior"]:
                    changed.append({"key": key, "title": t["title"],
                                    "from_desc": f["description"], "to_desc": t["description"],
                                    "from_eb": f["expected_behavior"], "to_eb": t["expected_behavior"]})
                else:
                    unchanged_count += 1

        return JsonResponse({
            "from_version": from_ver,
            "to_version": to_ver,
            "added": added, "removed": removed, "changed": changed,
            "unchanged_count": unchanged_count,
        })


class ScenarioExportView(ProjectMixin, View):
    def get(self, request, set_id):
        sset = ScenarioSet.objects.filter(pk=set_id, project=request.project).first()
        if not sset:
            return JsonResponse({"error": "Not found"}, status=404)
        latest = sset.versions.order_by("-version").first()
        scenarios = [
            {"key": it.scenario.key, "title": it.scenario.title,
             "description": it.revision.description, "category": it.scenario.category,
             "expected_behavior": it.revision.expected_behavior or [],
             "test_prompt": it.revision.test_prompt or ""}
            for it in latest.items.select_related("scenario", "revision")
        ] if latest else []
        return JsonResponse({"set_name": sset.name, "scenarios": scenarios})


class ScenarioImportView(ProjectMixin, View):
    def post(self, request, set_id):
        blocked = _require_writable_project(request)
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
                )
                new_ids.append(scenario.id)
            # Auto-publish after import (include newly imported scenarios)
            _publish_new_version(sset, request.user, extra_scenario_ids=new_ids)
        except Exception as e:  # noqa: BLE001 - surface any import/publish failure to the user
            return JsonResponse({"error": str(e)}, status=400)
        return redirect(f"/scenarios/?set={set_id}")


# ─── Models ──────────────────────────────────────────────────────────────────

class ModelsView(ProjectMixin, TemplateView):
    template_name = "models.html"

    def get_context_data(self, **kw):
        from django.db.models import Q

        from model_registry.models import ModelConnection

        p = self.request.project
        highlight_id = self.request.GET.get("highlight")
        q = (self.request.GET.get("q") or "").strip()

        connections = ModelConnection.objects.filter(project=p).prefetch_related("models")
        if q:
            connections = connections.filter(
                Q(name__icontains=q) | Q(base_url__icontains=q) | Q(models__display_name__icontains=q)
            ).distinct()
        connections = connections.order_by("name")

        kw.update(
            connections=connections,
            highlight_id=highlight_id,
            search_query=q,
        )
        return super().get_context_data(**kw)

    def post(self, request, *args, **kwargs):
        from model_registry.models import ModelConnection, RegisteredModel

        blocked = _require_writable_project(request)
        if blocked:
            return blocked
        p = request.project
        action = request.POST.get("action")
        error = None

        # ── Connection actions ──────────────────────────────────────────────
        if action == "add_connection":
            name = request.POST.get("conn_name", "").strip()
            base_url = request.POST.get("conn_base_url", "").strip()
            if not name or not base_url:
                error = "Connection name and Base URL are required."
            else:
                ModelConnection.objects.create(
                    project=p,
                    name=name,
                    base_url=base_url,
                    provider=request.POST.get("conn_provider", "openai"),
                    secret_reference=request.POST.get("conn_secret_ref", "").strip(),
                    api_key_direct=request.POST.get("conn_api_key", "").strip(),
                    enabled=True,
                    created_by=request.user,
                )
        elif action == "edit_connection":
            conn = ModelConnection.objects.filter(pk=request.POST.get("conn_id"), project=p).first()
            if not conn:
                error = "Connection not found."
            else:
                conn.name = request.POST.get("conn_name", conn.name).strip()
                conn.base_url = request.POST.get("conn_base_url", conn.base_url).strip()
                conn.provider = request.POST.get("conn_provider", conn.provider)
                conn.secret_reference = request.POST.get("conn_secret_ref", "").strip()
                new_key = request.POST.get("conn_api_key", "").strip()
                if new_key:
                    conn.api_key_direct = new_key
                conn.enabled = request.POST.get("conn_enabled") == "1"
                conn.save()
        elif action == "add_model":
            conn = ModelConnection.objects.filter(pk=request.POST.get("model_conn_id"), project=p).first()
            if not conn:
                error = "Connection not found."
            else:
                model_id = request.POST.get("model_id", "").strip()
                display_name = request.POST.get("model_display_name", model_id).strip()
                if not model_id:
                    error = "Model ID is required."
                else:
                    RegisteredModel.objects.update_or_create(
                        connection=conn, model_id=model_id,
                        defaults={"project": p, "display_name": display_name, "enabled": True},
                    )
        elif action == "edit_model":
            rm = RegisteredModel.objects.filter(pk=request.POST.get("rm_id"), project=p).first()
            if rm:
                new_name = request.POST.get("model_display_name", "").strip()
                new_id = request.POST.get("model_id_new", "").strip()
                if new_name:
                    rm.display_name = new_name
                if new_id and new_id != rm.model_id:
                    rm.model_id = new_id
                rm.save()
        elif action == "delete_model":
            rm = RegisteredModel.objects.filter(pk=request.POST.get("rm_id"), project=p).first()
            if rm:
                try:
                    rm.delete()
                except (ProtectedError, RestrictedError):
                    # AuditRun pins models via RESTRICT FKs; deleting a model
                    # referenced by an audit run would break the immutable record.
                    error = f"Cannot delete '{rm.display_name}': it is referenced by audit runs or schedules."
        return self.render_to_response(self.get_context_data(error=error))


class ConnectionDeleteView(ProjectMixin, View):
    def post(self, request, conn_id):
        from model_registry.models import ModelConnection

        blocked = _require_writable_project(request)
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
                    "Cannot delete: this connection's models are referenced by audit runs or schedules.",
                )
        return redirect("/models/")


class DiscoverModelsView(ProjectMixin, View):
    """Proxy GET {base_url}/models to auto-discover available models."""

    def post(self, request):
        import json as _json
        import urllib.error
        import urllib.request

        base_url = (request.POST.get("base_url") or "").strip().rstrip("/")
        api_key = (request.POST.get("api_key_direct") or "").strip()
        provider = (request.POST.get("provider") or "openai").strip()

        if not base_url:
            return JsonResponse({"error": "Base URL is required."}, status=400)

        # Build the models endpoint URL
        if provider == "openai" or "/v1" in base_url:
            url = f"{base_url}/models"
        elif provider == "anthropic":
            url = f"{base_url}/v1/models"
        else:
            url = f"{base_url}/models"

        headers = {"Accept": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        try:
            req = urllib.request.Request(url, headers=headers, method="GET")
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = _json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode()[:200]
            except Exception:  # noqa: BLE001,S110 - body read is best-effort
                pass
            return JsonResponse({"error": f"HTTP {e.code}: {body or e.reason}"}, status=502)
        except Exception as e:  # noqa: BLE001 - surface any upstream failure to the user
            return JsonResponse({"error": str(e)}, status=502)

        # Normalize response — OpenAI-compatible: {"data": [{"id": "...", ...}]}
        models = []
        if isinstance(data, dict) and "data" in data:
            for item in data["data"]:
                mid = item.get("id") or item.get("model") or ""
                if mid:
                    models.append(mid)
        elif isinstance(data, list):
            for item in data:
                if isinstance(item, str):
                    models.append(item)
                elif isinstance(item, dict):
                    mid = item.get("id") or item.get("model") or ""
                    if mid:
                        models.append(mid)

        models.sort()
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
        # Per-run link metadata
        run_meta = []
        for r in raw["runs"]:
            run_obj = AuditRun.objects.filter(id=r["id"]).select_related(
                "target_model", "auditor_model", "judge_model",
                "scenario_set_version__scenario_set"
            ).first()
            run_meta.append({
                "id": r["id"],
                "target_model_id": run_obj.target_model_id if run_obj else None,
                "auditor_model_id": run_obj.auditor_model_id if run_obj else None,
                "judge_model_id": run_obj.judge_model_id if run_obj else None,
                "scenario_set_id": run_obj.scenario_set_version.scenario_set_id if run_obj and run_obj.scenario_set_version else None,
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


class AuditResultsFragmentView(ProjectMixin, View):
    """GET /audits/<id>/results-fragment/ — the Results list alone, for live refresh."""

    def get(self, request, run_id):
        from django.template.loader import render_to_string

        run = get_object_or_404(
            AuditRun.objects.select_related("scenario_set_version"), pk=run_id, project=request.project
        )
        html = render_to_string(
            "partials/audit_results.html", {"run": run, "results": _result_rows(run)}, request=request
        )
        return HttpResponse(html)


class AuditDetailView(ProjectMixin, DetailView):
    template_name = "audit_detail.html"
    context_object_name = "run"
    pk_url_kwarg = "run_id"
    queryset = AuditRun.objects.select_related(
        "scenario_set_version__scenario_set", "target_model", "auditor_model", "judge_model", "schedule"
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
        if run.status not in ("completed", "failed", "cancelled"):
            from audits.events import progress_snapshot

            snap_events, snap_last_id = progress_snapshot(run.id)
            ctx["progress_snapshot_json"] = json.dumps(snap_events)
            ctx["progress_last_event_id"] = snap_last_id
        ctx["set_id"] = set_id
        ctx["progress_pct"] = (run.completed_scenarios * 100 // run.total_scenarios) if run.total_scenarios else 0
        ctx["stages"] = ["queued", "preparing", "target_execution", "auditing", "judging", "aggregation", "completed"]
        if run.started_at and run.finished_at:
            total = int((run.finished_at - run.started_at).total_seconds())
            if total < 60:
                ctx["duration"] = f"{total} sec"
            elif total < 3600:
                ctx["duration"] = f"{total // 60} min"
            else:
                ctx["duration"] = f"{total // 3600} hr {total % 3600 // 60} min"
        from audits.scheduling import has_write_role

        ctx["can_schedule"] = has_write_role(self.request.user, self.request.project)
        return ctx


class AuditCancelView(ProjectMixin, View):
    def post(self, request, run_id):
        blocked = _require_writable_project(request)
        if blocked:
            return blocked
        run = AuditRun.objects.filter(pk=run_id, project=request.project).first()
        if run and run.status not in (AuditRun.Status.COMPLETED, AuditRun.Status.FAILED, AuditRun.Status.CANCELLED):
            run.status = AuditRun.Status.CANCELLED
            if run.finished_at is None:
                run.finished_at = timezone.now()
            run.save(update_fields=["status", "finished_at"])
            # Terminal event so live progress ends; running scenarios stop at
            # their next repetition (they poll the durable flag).
            from audits.events import append_event
            append_event(run.pk, "_run", "run_cancelled", {"by": request.user.username})
        return redirect(f"/audits/{run_id}/")


class AuditArchiveView(ProjectMixin, View):
    """Toggle the soft-archive flag on a run. Project-scoped: runs outside
    the active project are invisible (404)."""

    def post(self, request, run_id):
        blocked = _require_writable_project(request)
        if blocked:
            return blocked
        run = AuditRun.objects.filter(pk=run_id, project=request.project).first()
        if run:
            run.archived = not run.archived
            run.save(update_fields=["archived"])
        return redirect(request.META.get("HTTP_REFERER") or f"/audits/{run_id}/")


class AuditRenameView(ProjectMixin, View):
    """Rename an audit run. The name is a display label only; it does not
    affect the frozen reproducibility manifest."""

    def post(self, request, run_id):
        blocked = _require_writable_project(request)
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
        return redirect(f"/audits/{run_id}/")


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
    return {
        "index": index,
        "severity": rep.get("severity", ""),
        "summary": rep.get("summary", ""),
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


class ScenarioResultDetailView(ProjectMixin, TemplateView):
    template_name = "scenario_result_detail.html"

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

class AuditExportView(ProjectMixin, View):
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


class DashboardExportView(ProjectMixin, View):
    """Export dashboard runs as CSV, respecting ?status= filter."""

    def get(self, request):
        qs = AuditRun.objects.filter(project=request.project).select_related(
            "scenario_set_version__scenario_set"
        ).order_by("-created_at")
        status = request.GET.get("status", "")
        if status == "archived":
            qs = qs.filter(archived=True)
        else:
            qs = qs.filter(archived=False)
            if status == "active":
                qs = qs.exclude(status__in=["completed", "failed", "cancelled"])
            elif status in ("completed", "failed", "cancelled"):
                qs = qs.filter(status=status)

        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["id", "name", "status", "scenario_set", "created_at", "started_at", "finished_at"])
        for run in qs:
            set_name = run.scenario_set_version.scenario_set.name if run.scenario_set_version else ""
            writer.writerow([
                run.id,
                run.name,
                run.status,
                set_name,
                run.created_at.isoformat() if run.created_at else "",
                run.started_at.isoformat() if run.started_at else "",
                run.finished_at.isoformat() if run.finished_at else "",
            ])

        response = HttpResponse(buf.getvalue(), content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = 'attachment; filename="dashboard_runs.csv"'
        return response
