"""Experiments: a grid of audit runs launched together.

The design form lets every input take one or more values. Inputs with one
value are *fixed*; inputs with several are *factors* (what the experiment
compares). The cartesian product of all values is the list of runs, each an
ordinary frozen AuditRun linked to the Experiment. A design that expands to a
single run launches it directly, without an Experiment.
"""
from __future__ import annotations

import itertools
import json

from django.db import transaction

from audits.models import AuditRun, Experiment
from audits.monitors import pass_counts, wilson
from audits.services import frozen_judge, frozen_name

MAX_RUNS_PER_EXPERIMENT = 50

# Inputs the design form can vary, in display order, with the labels users see.
DESIGN_AXES = {
    "scenario_set": "Scenario set",
    "target": "Target",
    "auditor": "Auditor",
    "judge_model": "Judge model",
    "judge": "Judge",
    "max_turns": "Max turns",
    "language": "Language",
}
# Everything an experiment can compare: the design axes plus per-run edits
# made on the review screen (repetitions, system prompt, generation config).
FACTORS = {**DESIGN_AXES, "n_repetitions": "Repetitions", "system_prompt": "System prompt", "params": "Parameters"}
# Axes whose values are registered models (axis -> form field and label);
# "judge" values are JudgeVersions.
_MODEL_AXES = {"target": ("target_model", "target"), "auditor": ("auditor_model", "auditor"),
               "judge_model": ("judge_model", "judge")}
_MODEL_ROLES = tuple(_MODEL_AXES)
_ROLE_AXES = ("target", "auditor", "judge_model", "judge")
# Generation keys with their own form fields (not "parameters").
_FORM_KEYS = ("max_turns", "language", "n_repetitions", "system_prompt")


class DesignError(ValueError):
    """A design the user must fix (shown on the form)."""


def _split_values(raw: str, cast):
    """'3, 5' -> [3, 5]; '' -> [None] (engine default). Order kept, duplicates dropped."""
    out = []
    for part in (raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            value = cast(part)
        except ValueError as exc:
            raise DesignError(f"'{part}' is not a valid value.") from exc
        if value not in out:
            out.append(value)
    return out or [None]


def _positive_int(value: str) -> int:
    n = int(value)
    if not 1 <= n <= 50:
        raise ValueError(value)
    return n


def parse_design(post, project) -> dict:
    """Validate the design form into lists of values per input.

    Raises DesignError with a user-facing message.
    """
    from judges.models import Judge, JudgeVersion
    from model_registry.models import RegisteredModel
    from scenarios.models import ScenarioSet, ScenarioSetVersion

    # Scenario versions: explicit version ids ("scenario_version", several may
    # belong to one set to compare versions), "latest:<set id>" (always latest:
    # unpinned, so repeating runs follow new versions) and/or set ids
    # ("scenario_set", shorthand for the set's current latest, pinned).
    raw = [v for v in post.getlist("scenario_version") if v]
    follow_set_ids = [v.split(":", 1)[1] for v in raw if v.startswith("latest:")]
    version_ids = [v for v in raw if not v.startswith("latest:")]
    set_ids = [s for s in post.getlist("scenario_set") if s]
    if not version_ids and not set_ids and not follow_set_ids:
        raise DesignError("Pick at least one scenario set.")
    chosen = list(
        ScenarioSetVersion.objects.filter(pk__in=version_ids, scenario_set__project=project).select_related("scenario_set")
    )
    if len(chosen) != len(set(version_ids)):
        raise DesignError("A selected scenario set version is not in this workspace.")
    sets = list(ScenarioSet.objects.filter(project=project, pk__in=set_ids))
    if len(sets) != len(set(set_ids)):
        raise DesignError("A selected scenario set is not in this workspace.")
    for sset in sets:
        latest = sset.versions.select_related("scenario_set").order_by("-version").first()
        if latest is None:
            raise DesignError(f"Scenario set '{sset.name}' has no published version yet.")
        chosen.append(latest)
    for sset in ScenarioSet.objects.filter(project=project, pk__in=follow_set_ids):
        latest = sset.versions.select_related("scenario_set").order_by("-version").first()
        if latest is None:
            raise DesignError(f"Scenario set '{sset.name}' has no published version yet.")
        latest.follow_latest = True  # distinct instance: runs today's latest, monitors unpinned
        chosen.append(latest)
    if len({s for s in follow_set_ids}) != sum(1 for v in chosen if is_follow(v)):
        raise DesignError("A selected scenario set is not in this workspace.")
    versions = sorted(
        {(v.pk, is_follow(v)): v for v in chosen}.values(),
        key=lambda v: (v.scenario_set.name, is_follow(v), v.version),
    )

    models = {}
    for axis, (field, label) in _MODEL_AXES.items():
        ids = [m for m in post.getlist(field) if m]
        if not ids:
            raise DesignError(f"Pick at least one {label} model.")
        found = {str(m.pk): m for m in RegisteredModel.objects.filter(project=project, pk__in=ids).select_related("connection")}
        missing = [i for i in ids if i not in found]
        if missing:
            raise DesignError(f"A selected {label} model is not in this workspace.")
        models[axis] = [found[i] for i in dict.fromkeys(ids)]

    # Judges: version ids, and/or "latest:<judge id>" (always latest: unpinned,
    # so repeating runs follow new versions).
    raw = [v for v in dict.fromkeys(post.getlist("judge")) if v]
    if not raw:
        raise DesignError("Pick at least one judge.")
    follow_ids = [v.split(":", 1)[1] for v in raw if v.startswith("latest:")]
    pinned_ids = [v for v in raw if not v.startswith("latest:")]
    judges = list(
        JudgeVersion.objects.filter(pk__in=pinned_ids, judge__project=project).select_related("judge")
    )
    follow = list(Judge.objects.filter(pk__in=follow_ids, project=project))
    if len(judges) != len(pinned_ids) or len(follow) != len(follow_ids):
        raise DesignError("A selected judge is not in this workspace.")
    for judge in follow:
        latest = judge.versions.select_related("judge").order_by("-version").first()
        if latest is None:
            raise DesignError(f"Judge “{judge.name}” has no version.")
        latest.follow_latest = True
        judges.append(latest)
    judges.sort(key=lambda v: (v.judge.name, is_follow(v), v.version))

    n_reps_raw = (post.get("n_repetitions") or "").strip()
    try:
        n_reps = int(n_reps_raw) if n_reps_raw else None
    except ValueError as exc:
        raise DesignError("Repetitions must be a whole number.") from exc
    if n_reps is not None and not 1 <= n_reps <= 20:
        raise DesignError("Repetitions must be between 1 and 20.")

    gen_config = None
    gen_raw = (post.get("gen_config_json") or "").strip()
    if gen_raw and gen_raw != "{}":
        try:
            gen_config = json.loads(gen_raw)
        except json.JSONDecodeError as exc:
            raise DesignError(f"Invalid generation config JSON: {exc}") from exc
        if not isinstance(gen_config, dict):
            raise DesignError("Generation config must be a JSON object.")

    try:
        max_turns = _split_values(post.get("max_turns", ""), _positive_int)
    except DesignError as exc:
        raise DesignError(f"Max turns: {exc} Use whole numbers from 1 to 50.") from exc
    languages = _split_values(post.get("language", ""), str)

    # The target's system prompt lives in the generation config (the engine
    # reads it there); the field wins over a key typed into the JSON.
    system_prompt = (post.get("system_prompt") or "").strip()
    if system_prompt:
        gen_config = {**(gen_config or {}), "system_prompt": system_prompt}

    return {
        "scenario_set": versions,
        "target": models["target"],
        "auditor": models["auditor"],
        "judge_model": models["judge_model"],
        "judge": judges,
        "max_turns": max_turns,
        "language": languages,
        "n_repetitions": n_reps if n_reps and n_reps > 1 else None,
        "gen_config": gen_config or None,
    }


def is_follow(version) -> bool:
    """Whether this scenario set / judge version was picked as "Always latest"."""
    return bool(getattr(version, "follow_latest", False))


def varied_factors(design: dict) -> list[str]:
    return [key for key in DESIGN_AXES if len(design[key]) > 1]


def run_count(design: dict) -> int:
    n = 1
    for key in DESIGN_AXES:
        n *= len(design[key])
    return n


def expand(design: dict) -> list[dict]:
    """Every combination of the design's values, as run specs."""
    if run_count(design) > MAX_RUNS_PER_EXPERIMENT:
        raise DesignError(
            f"This design makes {run_count(design)} runs; the limit is {MAX_RUNS_PER_EXPERIMENT}. "
            "Fix more inputs to one value, or split it into several experiments."
        )
    specs = []
    for combo in itertools.product(*(design[key] for key in DESIGN_AXES)):
        spec = dict(zip(DESIGN_AXES, combo, strict=True))
        spec["n_repetitions"] = design["n_repetitions"]
        spec["gen_config"] = design["gen_config"]
        specs.append(spec)
    return specs


def factor_value_label(key: str, value) -> str:
    if value is None:
        return "default"
    if key == "scenario_set":
        if is_follow(value):
            return f"{value.scenario_set.name} (always latest, now v{value.version})"
        return value.label
    if key == "judge":
        if is_follow(value):
            return f"{value.judge.name} (always latest, now v{value.version})"
        return value.label
    if key in _MODEL_ROLES:
        return value.display_name
    return str(value)


def spec_label(spec: dict, factors: list[str]) -> str:
    """Short name for one run: its values of the varied factors."""
    return " · ".join(factor_value_label(k, spec[k]) for k in factors) or "Run"


def spec_warnings(spec: dict) -> list[str]:
    warnings = []
    if spec["judge_model"].pk == spec["target"].pk:
        warnings.append("Judge model is the target model: it grades itself.")
    for role, model in (("Target", spec["target"]), ("Auditor", spec["auditor"]), ("Judge", spec["judge_model"])):
        if not model.has_key:
            warnings.append(f"{role} model has no API key set.")
        elif not (model.enabled and model.connection.enabled):
            warnings.append(f"{role} model is disabled.")
    return warnings


def design_warnings(factors: list[str]) -> list[str]:
    """Experiment-level cautions about how results can be read."""
    roles = [f for f in factors if f in _ROLE_AXES]
    warnings = []
    if "target" in roles and len(roles) > 1:
        others = " and ".join(FACTORS[r].lower() for r in roles if r != "target")
        warnings.append(
            f"Target and {others} both vary, so a difference between runs cannot be pinned on one of them. "
            "Keep the judge (and auditor) fixed to compare targets fairly."
        )
    return warnings


def default_experiment_name(design: dict, factors: list[str]) -> str:
    sets = ", ".join(factor_value_label("scenario_set", v) for v in design["scenario_set"][:2])
    if len(design["scenario_set"]) > 2:
        sets += ", …"
    compared = " × ".join(FACTORS[f].lower() for f in factors if f != "scenario_set")
    return f"{sets} — {compared}" if compared else sets


def params_label(params: dict | None) -> str:
    """Compact, stable text for a generation config (form-managed keys excluded)."""
    rest = {k: v for k, v in (params or {}).items() if k not in _FORM_KEYS}
    return json.dumps(rest, sort_keys=True, separators=(", ", ": ")) if rest else "default"


def prompt_label(prompt: str | None) -> str:
    """Short text for a system prompt in tables: its first line, clipped."""
    first = (prompt or "").strip().splitlines()[0] if (prompt or "").strip() else ""
    if not first:
        return "none"
    return first if len(first) <= 48 else first[:47] + "…"


def factors_from_runs(runs: list[dict]) -> list[str]:
    """Which inputs actually differ across the runs about to be launched.

    Computed from the final runs (after review edits and duplicates), so a
    duplicated run with another language makes "language" a factor.
    """
    def value(run, key):
        if key == "scenario_set":
            return (run["version"].pk, is_follow(run["version"]))
        if key == "judge":
            return (run["judge"].pk, is_follow(run["judge"]))
        if key in _MODEL_ROLES:
            return run[key].pk
        if key == "system_prompt":
            return (run["gen_config"] or {}).get("system_prompt") or ""
        if key == "params":
            return params_label(run["gen_config"])
        return run[key]

    return [key for key in FACTORS if len({value(r, key) for r in runs}) > 1]


def spec_to_row(spec: dict) -> str:
    """Serialise the non-editable part of a spec for the review form."""
    return json.dumps({
        "v": spec["scenario_set"].pk,
        "f": 1 if is_follow(spec["scenario_set"]) else 0,
        "t": spec["target"].pk,
        "a": spec["auditor"].pk,
        "jm": spec["judge_model"].pk,
        "j": spec["judge"].pk,
        "jf": 1 if is_follow(spec["judge"]) else 0,
    })


def launch_experiment(*, project, user, name: str, runs: list[dict], repeat: dict | None = None) -> Experiment:
    """Create the Experiment and its frozen runs, then submit them.

    ``runs`` items: {"name", "version", "target", "auditor", "judge_model", "judge",
    "max_turns", "language", "n_repetitions", "gen_config"}. With ``repeat``
    (see audits.monitors.parse_repeat) each run setup also gets a Monitor
    linked to the experiment; runs start now unless repeat["start"] is "at".
    Creation is atomic (all or nothing); submission happens after commit, and
    a run that cannot be submitted stays queued for the sweeper to retry.
    """
    from accounts.models import ProjectMembership
    from audits.monitors import create_monitor
    from audits.services import create_audit_run, submit_audit_run
    from scenarios.services import require_project_role

    # Checked up front: with a later start no run is created now, so the check
    # inside create_audit_run would not happen before monitors are made.
    require_project_role(user, project, ProjectMembership.Role.ADMIN, ProjectMembership.Role.AUDITOR)
    if not runs:
        raise DesignError("Select at least one run to launch.")
    if len(runs) > MAX_RUNS_PER_EXPERIMENT:
        raise DesignError(f"At most {MAX_RUNS_PER_EXPERIMENT} runs per experiment.")
    with transaction.atomic():
        experiment = Experiment.objects.create(
            project=project, name=name.strip() or "Experiment", factors=factors_from_runs(runs), created_by=user
        )
        run_now = not repeat or repeat["start"] == "now"
        created = [
            create_audit_run(
                project=project,
                user=user,
                name=r["name"],
                scenario_set_version=r["version"],
                target_model=r["target"],
                auditor_model=r["auditor"],
                judge_model=r["judge_model"],
                judge=r["judge"],
                max_turns_override=r["max_turns"],
                language_override=r["language"],
                n_repetitions_override=r["n_repetitions"],
                gen_config_override=r["gen_config"],
                experiment=experiment,
            )
            for r in runs
        ] if run_now else []
        if repeat:
            for i, r in enumerate(runs):
                create_monitor(
                    project=project,
                    user=user,
                    name=f"{experiment.name} · {r['name']}",
                    run=r,
                    repeat=repeat,
                    experiment=experiment,
                    first_point=created[i] if created else None,
                )
    # Interleaved order already (cartesian product), so a slow provider does
    # not stall one factor level for the whole queue.
    for run in created:
        submit_audit_run(run)
    return experiment


# ─── Results ─────────────────────────────────────────────────────────────────

def run_factor_values(run: AuditRun) -> dict:
    params = run.generation_parameters_snapshot or {}
    return {
        "scenario_set": run.scenario_set_version.label,
        "target": frozen_name(run, "target"),
        "auditor": frozen_name(run, "auditor"),
        "judge_model": frozen_name(run, "judge"),
        "judge": frozen_judge(run)["label"],
        "max_turns": str(params.get("max_turns") or "default"),
        "language": params.get("language") or "default",
        "n_repetitions": f"{params.get('n_repetitions') or 1}×",
        "system_prompt": prompt_label(params.get("system_prompt")),
        "params": params_label(params),
    }


def experiment_pivot(runs: list[AuditRun], row_factor: str | None, col_factor: str | None) -> dict:
    """Pass rate per (row value, column value), pooling runs that share a cell."""
    counts = pass_counts([r.id for r in runs])
    rows, cols, cells = [], [], {}
    for run in runs:
        values = run_factor_values(run)
        rv = values[row_factor] if row_factor else "All runs"
        cv = values[col_factor] if col_factor else "Pass rate"
        if rv not in rows:
            rows.append(rv)
        if cv not in cols:
            cols.append(cv)
        cell = cells.setdefault((rv, cv), {"k": 0, "n": 0, "runs": []})
        cell["k"] += counts[run.id]["k"]
        cell["n"] += counts[run.id]["n"]
        cell["runs"].append(run)
    table = []
    for rv in rows:
        line = []
        for cv in cols:
            cell = cells.get((rv, cv))
            if cell and cell["n"]:
                lo, hi = wilson(cell["k"], cell["n"])
                cell.update(rate=cell["k"] / cell["n"] * 100, lo=lo * 100, hi=hi * 100, span=(hi - lo) * 100)
            line.append(cell)
        table.append({"label": rv, "cells": line})
    return {"cols": cols, "rows": table}
