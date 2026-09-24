"""Server-rendered UI — Django CBVs + Forms + HTMX."""
import csv
import hashlib
import io
import json

from django.contrib import messages
from django.contrib.auth import login, logout
from django.contrib.auth.forms import AuthenticationForm
from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.utils import timezone
from django.views.generic import CreateView, DetailView, ListView, TemplateView, View

from audits.events import ScenarioResult
from audits.models import AuditRun
from audits.services import create_audit_run, submit_audit_run
from audits.comparison import compare_runs
from model_registry.models import AuditProfile, ModelEndpoint
from scenarios.models import Scenario, ScenarioRevision, ScenarioSet, ScenarioSetVersionItem
from scenarios.services import publish_scenario_set_version


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
    def get(self, request, *args, **kwargs):
        return redirect("dashboard" if request.user.is_authenticated else "login")


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
        return super().get_context_data(**kw)

    def post(self, request, *args, **kwargs):
        form = AuthenticationForm(request, data=request.POST)
        if form.is_valid():
            login(request, form.get_user())
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


# ─── Dashboard ───────────────────────────────────────────────────────────────

class DashboardView(ProjectMixin, ListView):
    template_name = "dashboard.html"
    context_object_name = "runs"
    paginate_by = 25

    _SORT_WHITELIST = {"created_at", "-created_at", "status", "-id"}

    def get_queryset(self):
        from django.db.models import Q

        qs = AuditRun.objects.filter(project=self.request.project).select_related(
            "scenario_set_version__scenario_set"
        )
        # Status filter via ?status=active|completed|failed|cancelled
        status = self.request.GET.get("status", "")
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
        ctx["stats"] = {
            "total": base.count(),
            "active": base.exclude(status__in=["completed", "failed", "cancelled"]).count(),
            "completed": base.filter(status="completed").count(),
            "failed": base.filter(status="failed").count(),
        }
        ctx["current_status"] = self.request.GET.get("status", "")
        ctx["search_query"] = (self.request.GET.get("q") or "").strip()
        ctx["current_sort"] = self.request.GET.get("sort", "-created_at")
        return ctx


# ─── New Audit ───────────────────────────────────────────────────────────────

class NewAuditView(ProjectMixin, TemplateView):
    template_name = "new_audit.html"

    def get_context_data(self, **kw):
        p = self.request.project
        kw.update(
            sets=ScenarioSet.objects.filter(project=p),
            endpoints=ModelEndpoint.objects.filter(project=p).order_by("display_name"),
            profiles=AuditProfile.objects.filter(project=p).order_by("name"),
            error=None,
        )
        return super().get_context_data(**kw)

    def post(self, request, *args, **kwargs):
        p = request.project
        try:
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
            if gen_json_raw:
                try:
                    parsed = json.loads(gen_json_raw)
                    if not isinstance(parsed, dict):
                        raise ValueError("Must be a JSON object")
                    gen_config_override = parsed
                except Exception as e:
                    return self.render_to_response(self.get_context_data(error=f"Invalid generation config JSON: {e}"))

            run = create_audit_run(
                project=p,
                user=request.user,
                name=f"Audit {timezone.now():%Y-%m-%d %H:%M}",
                scenario_set_version=version,
                target_endpoint=ModelEndpoint.objects.get(pk=request.POST["target_endpoint"], project=p),
                auditor_endpoint=ModelEndpoint.objects.get(pk=request.POST["auditor_endpoint"], project=p),
                judge_endpoint=ModelEndpoint.objects.get(pk=request.POST["judge_endpoint"], project=p),
                audit_profile=AuditProfile.objects.filter(pk=request.POST.get("profile"), project=p).first() or None,
                max_turns_override=max_turns_override,
                language_override=language_override,
                n_repetitions_override=n_repetitions_override,
                gen_config_override=gen_config_override,
            )
            submit_audit_run(run)
            return redirect(f"/audits/{run.id}/")
        except Exception as e:
            return self.render_to_response(self.get_context_data(error=str(e)))


# ─── Queue ───────────────────────────────────────────────────────────────────

class QueueView(ProjectMixin, TemplateView):
    template_name = "queue.html"

    def get_context_data(self, **kw):
        runs = (
            AuditRun.objects.filter(project=self.request.project)
            .select_related("scenario_set_version__scenario_set")
            .order_by("-created_at")[:100]
        )
        active_statuses = ["queued", "preparing", "target_execution", "auditing", "judging", "aggregation", "report_generation"]
        now = timezone.now()
        active = []
        for r in runs:
            if r.status in active_statuses:
                r.elapsed = (now - r.started_at).total_seconds() if r.started_at else None
                active.append(r)
        kw.update(
            active=active,
            finished=[r for r in runs if r.status not in active_statuses],
        )
        return super().get_context_data(**kw)


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
        sset = ScenarioSet.objects.filter(pk=set_id, project=request.project).first()
        if sset:
            try:
                sset.delete()
                messages.success(request, "Scenario set deleted.")
            except Exception:
                messages.error(request, "Cannot delete: this set is referenced by audit runs.")
        return redirect("/scenarios/")


class ScenarioCreateView(ProjectMixin, View):
    def post(self, request):
        name = request.POST.get("name", "").strip()
        category = request.POST.get("category", "").strip()
        desc = request.POST.get("description", "")
        expected_behavior_raw = request.POST.get("expected_behavior", "").strip()
        expected_behavior = [line.strip() for line in expected_behavior_raw.splitlines() if line.strip()] if expected_behavior_raw else []
        set_id = request.POST.get("set_id", "").strip()
        if name:
            key = hashlib.sha256(name.encode()).hexdigest()[:12]
            scenario, created = Scenario.objects.get_or_create(
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
                l, t = latest_map[key], target_map[key]
                if l["description"] != t["description"] or l["expected_behavior"] != t["expected_behavior"]:
                    changed.append({"key": key, "title": t["title"],
                                    "latest_desc": l["description"], "target_desc": t["description"],
                                    "latest_eb": l["expected_behavior"], "target_eb": t["expected_behavior"]})
                else:
                    unchanged.append({"key": key, "title": t["title"]})

        return JsonResponse({
            "target_version": target_ver,
            "latest_version": latest_version.version,
            "added": added, "removed": removed, "changed": changed,
            "unchanged_count": len(unchanged),
        })

    def post(self, request, set_id):
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
        except Exception as e:
            return JsonResponse({"error": str(e)}, status=400)
        return redirect(f"/scenarios/?set={set_id}")


# ─── Models ──────────────────────────────────────────────────────────────────

class ModelsView(ProjectMixin, TemplateView):
    template_name = "models.html"

    def get_context_data(self, **kw):
        from django.db.models import Q

        p = self.request.project
        highlight_id = self.request.GET.get("highlight")
        q = (self.request.GET.get("q") or "").strip()
        endpoints = ModelEndpoint.objects.filter(project=p)
        if q:
            endpoints = endpoints.filter(Q(display_name__icontains=q) | Q(model_id__icontains=q))
        kw.update(
            endpoints=endpoints.order_by("display_name"),
            profiles=AuditProfile.objects.filter(project=p).order_by("name"),
            highlight_id=highlight_id,
            search_query=q,
            error=None,
        )
        return super().get_context_data(**kw)

    def post(self, request, *args, **kwargs):
        p = request.project
        action = request.POST.get("action")
        error = None
        if action == "add_endpoint":
            if not all([request.POST.get(k) for k in ("display_name", "base_url")]):
                error = "Name and URL are required."
            else:
                ModelEndpoint.objects.create(
                    project=p,
                    display_name=request.POST["display_name"].strip(),
                    base_url=request.POST["base_url"].strip(),
                    model_id=request.POST.get("model_id", "").strip(),
                    provider=request.POST.get("provider", "openai"),
                    secret_reference=request.POST.get("secret_reference", "").strip(),
                    api_key_direct=request.POST.get("api_key_direct", "").strip(),
                    enabled=True,
                    created_by=request.user,
                )
        elif action == "edit_endpoint":
            ep = ModelEndpoint.objects.filter(pk=request.POST.get("endpoint_id"), project=p).first()
            if not ep:
                error = "Endpoint not found."
            else:
                ep.display_name = request.POST.get("display_name", ep.display_name).strip()
                ep.base_url = request.POST.get("base_url", ep.base_url).strip()
                ep.model_id = request.POST.get("model_id", ep.model_id).strip()
                ep.provider = request.POST.get("provider", ep.provider)
                ep.secret_reference = request.POST.get("secret_reference", "").strip()
                new_key = request.POST.get("api_key_direct", "").strip()
                if new_key:  # empty means "keep existing" (form no longer echoes the stored key)
                    ep.api_key_direct = new_key
                ep.enabled = request.POST.get("enabled") == "1"
                ep.save()
        elif action == "edit_profile":
            prof = AuditProfile.objects.filter(pk=request.POST.get("profile_id"), project=p).first()
            if not prof:
                error = "Profile not found."
            else:
                prof.name = request.POST.get("profile_name", prof.name).strip()
                prof.max_turns = int(request.POST.get("max_turns", prof.max_turns))
                prof.temperature_target = float(request.POST.get("temp_target", prof.temperature_target))
                prof.temperature_auditor = float(request.POST.get("temp_auditor", prof.temperature_auditor))
                prof.temperature_judge = float(request.POST.get("temp_judge", prof.temperature_judge))
                mt_raw = (request.POST.get("max_tokens") or "").strip()
                if mt_raw:
                    prof.max_tokens = int(mt_raw)
                lang = request.POST.get("language", "").strip()
                prof.language = lang or None
                prof.save()
        elif action == "add_profile":
            if not request.POST.get("profile_name", "").strip():
                error = "Profile name is required."
            else:
                AuditProfile.objects.create(
                    project=p, name=request.POST["profile_name"].strip(),
                    max_turns=int(request.POST.get("max_turns", 5)),
                    temperature_target=float(request.POST.get("temp_target", 0.7)),
                    temperature_auditor=float(request.POST.get("temp_auditor", 0.2)),
                    temperature_judge=float(request.POST.get("temp_judge", 0.0)),
                    max_tokens=int(request.POST.get("max_tokens", 2048)),
                    created_by=request.user,
                )
        return self.render_to_response(self.get_context_data(error=error))


class ModelDeleteView(ProjectMixin, View):
    def post(self, request, endpoint_id):
        ModelEndpoint.objects.filter(pk=endpoint_id, project=request.project).delete()
        return redirect("/models/")


class ProfileDeleteView(ProjectMixin, View):
    def post(self, request, profile_id):
        prof = AuditProfile.objects.filter(pk=profile_id, project=request.project).first()
        if prof:
            if prof.audit_runs.exists():
                messages.error(request, f"Cannot delete '{prof.name}': it is referenced by audit runs.")
            else:
                name = prof.name
                prof.delete()
                messages.success(request, f"Audit profile '{name}' deleted.")
        return redirect("/models/")


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
                "target_endpoint", "auditor_endpoint", "judge_endpoint",
                "scenario_set_version__scenario_set"
            ).first()
            run_meta.append({
                "id": r["id"],
                "target_endpoint_id": run_obj.target_endpoint_id if run_obj else None,
                "auditor_endpoint_id": run_obj.auditor_endpoint_id if run_obj else None,
                "judge_endpoint_id": run_obj.judge_endpoint_id if run_obj else None,
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
                except Exception as e:
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
            except Exception as e:
                error = str(e)
        return self.render_to_response(self.get_context_data(result=result, error=error, selected_ids=ids))


# ─── Audit Detail ────────────────────────────────────────────────────────────

class AuditDetailView(ProjectMixin, DetailView):
    template_name = "audit_detail.html"
    context_object_name = "run"
    pk_url_kwarg = "run_id"
    queryset = AuditRun.objects.select_related(
        "scenario_set_version__scenario_set", "target_endpoint", "auditor_endpoint", "judge_endpoint"
    )

    def get_queryset(self):
        return self.queryset.filter(project=self.request.project)

    def get_context_data(self, **kw):
        ctx = super().get_context_data(**kw)
        run = self.object
        set_id = run.scenario_set_version.scenario_set_id
        items = {str(vi.pk): vi for vi in ScenarioSetVersionItem.objects.filter(version=run.scenario_set_version).select_related("scenario")}
        n_reps = int((run.generation_parameters_snapshot or {}).get("n_repetitions") or 1)
        results = []
        for sr in ScenarioResult.objects.filter(run_id=run.id).order_by("id"):
            item = items.get(sr.version_item_id)
            r = sr.result or {}
            # When n_repetitions > 1, the result dict has "reps" + "aggregated_severity"
            if n_reps > 1 and "reps" in r:
                severity = r.get("aggregated_severity", sr.status)
                agreement = r.get("agreement_rate")
                sev_dist = r.get("severity_distribution", {})
            else:
                severity = r.get("severity", sr.status)
                agreement = None
                sev_dist = None
            results.append({
                "result_id": sr.pk,
                "scenario_name": item.scenario.title if item else sr.version_item_id,
                "scenario_id": item.scenario_id if item else None,
                "set_id": set_id,
                "severity": severity,
                "summary": r.get("summary", ""),
                "status": sr.status,
                "agreement_rate": agreement,
                "severity_distribution": sev_dist,
                "n_reps": n_reps if (n_reps > 1 and "reps" in r) else None,
            })
        ctx["results"] = results
        ctx["set_id"] = set_id
        ctx["stages"] = ["queued", "preparing", "target_execution", "auditing", "judging", "aggregation", "completed"]
        return ctx


class AuditCancelView(ProjectMixin, View):
    def post(self, request, run_id):
        run = AuditRun.objects.filter(pk=run_id, project=request.project).first()
        if run and run.status not in (AuditRun.Status.COMPLETED, AuditRun.Status.FAILED, AuditRun.Status.CANCELLED):
            run.status = AuditRun.Status.CANCELLED
            run.save(update_fields=["status"])
        return redirect(f"/audits/{run_id}/")


# ─── Scenario Result Detail ──────────────────────────────────────────────────

class ScenarioResultDetailView(ProjectMixin, TemplateView):
    template_name = "scenario_result_detail.html"

    def get_context_data(self, **kw):
        run_id = self.kwargs["run_id"]
        result_id = self.kwargs["result_id"]
        run = get_object_or_404(AuditRun, pk=run_id, project=self.request.project)
        sr = get_object_or_404(ScenarioResult, pk=result_id, run_id=run_id)

        # Resolve scenario name via version item
        item = ScenarioSetVersionItem.objects.filter(pk=sr.version_item_id).select_related("scenario").first()
        scenario_name = item.scenario.title if item else f"Scenario {sr.version_item_id}"

        # Parse result JSON into structured sections
        result_data = sr.result or {}

        # Detect repeated format (n_repetitions > 1)
        is_repeated = "reps" in result_data and isinstance(result_data.get("reps"), list)
        reps = result_data.get("reps", []) if is_repeated else []
        aggregated_severity = result_data.get("aggregated_severity", "") if is_repeated else ""
        raw_agreement = result_data.get("agreement_rate") if is_repeated else None
        # Convert fraction (0.6667) to percentage (66.67) for display
        agreement_rate = round(raw_agreement * 100, 1) if raw_agreement is not None else None
        low_agreement = (agreement_rate is not None and agreement_rate < 80)
        severity_distribution = result_data.get("severity_distribution", {}) if is_repeated else {}
        n_reps = len(reps) if is_repeated else 0

        if is_repeated:
            # Show the modal rep's details as the "primary" view
            primary_rep = reps[0] if reps else {}
            severity = aggregated_severity or primary_rep.get("severity", sr.status)
        else:
            primary_rep = result_data
            severity = result_data.get("severity", sr.status)

        conversation = primary_rep.get("conversation", [])
        raw_issues = primary_rep.get("issues_found", primary_rep.get("issues", []))
        # Normalize issues to dicts with 'description' key for template safety
        issues = []
        for iss in raw_issues:
            if isinstance(iss, str):
                issues.append({"description": iss})
            elif isinstance(iss, dict):
                issues.append(iss)
            else:
                issues.append({"description": str(iss)})
        rationale = primary_rep.get("rationale", primary_rep.get("evidence", primary_rep.get("judge_rationale", "")))
        summary = primary_rep.get("summary", "")

        # Per-rep summary table for repeated results
        rep_summaries = []
        if is_repeated:
            for i, rep in enumerate(reps):
                rep_summaries.append({
                    "index": i + 1,
                    "severity": rep.get("severity", ""),
                    "tokens": rep.get("tokens_used", rep.get("total_tokens", "")),
                    "latency_ms": rep.get("latency_ms", ""),
                })

        # Collect remaining keys not already displayed as named sections
        known_keys = {"conversation", "issues_found", "issues", "rationale", "evidence", "judge_rationale", "severity", "summary", "reps", "aggregated_severity", "agreement_rate", "severity_distribution", "n_repetitions"}
        other_keys = {k: v for k, v in primary_rep.items() if k not in known_keys}

        kw.update(
            run=run,
            sr=sr,
            scenario_name=scenario_name,
            severity=severity,
            summary=summary,
            conversation=conversation,
            issues=issues,
            rationale=rationale,
            other_keys=other_keys,
            raw_json=json.dumps(result_data, indent=2, ensure_ascii=False) if result_data else "",
            is_repeated=is_repeated,
            reps=reps,
            rep_summaries=rep_summaries,
            aggregated_severity=aggregated_severity,
            agreement_rate=agreement_rate,
            low_agreement=low_agreement,
            severity_distribution=severity_distribution,
            n_reps=n_reps,
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
