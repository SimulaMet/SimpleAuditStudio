"""Judge rubrics (from SimpleAudit), versioning, cloning and run snapshots."""
from __future__ import annotations

from functools import cache

from django.db import transaction
from django.db.models import Max

from judges.models import Judge, JudgeVersion

DEFAULT_RUBRIC_LABEL = "SimpleAudit default"

# What a rubric's grade looks like, for pickers and result pages.
OUTPUT_LABELS = {
    "severity": "Severity",
    "score": "Score 1–10",
    "binary": "Yes / no",
    "checklist": "Checklist",
}

# Starter judges for a new workspace: general-purpose rubrics (the domain
# rubrics stay available when creating a judge).
STARTER_RUBRICS = ("safety", "harm", "helpfulness", "factuality", "abstention", "checklist")


def _output_kind(config: dict) -> str:
    """The config's declared ``output`` (SimpleAudit 0.2.2+), else inferred from its schema."""
    if config.get("output") in OUTPUT_LABELS:
        return config["output"]
    if config.get("requires_expected_behavior"):
        return "checklist"
    props = (config.get("response_schema") or {}).get("properties") or {}
    if "score" in props:
        return "score"
    if props and "severity" not in props:
        return "binary"
    return "severity"


@cache
def rubrics() -> dict[str, dict]:
    """Built-in rubrics by key: name, description, output kind, source and default prompts.

    ``""`` is SimpleAudit's default judge (no named config).
    """
    catalogue = {
        "": {
            "key": "",
            "name": DEFAULT_RUBRIC_LABEL,
            "description": "SimpleAudit's built-in safety judge: harm, boundaries, accuracy, transparency and "
                           "manipulation resistance, graded by severity.",
            "output": "severity",
            "source_url": "",
            "probe_prompt": "",
            "judge_prompt": "",
        }
    }
    try:
        from simpleaudit.judges import JUDGE_CONFIGS
    except ImportError:  # engine not installed (web-only tooling): default only
        return catalogue
    try:   # SimpleAudit 0.2.2+ exports the default judge's prompts
        from simpleaudit.judges import DEFAULT_JUDGE

        catalogue[""].update(probe_prompt=DEFAULT_JUDGE["probe_prompt"], judge_prompt=DEFAULT_JUDGE["judge_prompt"])
    except ImportError:
        pass
    for key, config in JUDGE_CONFIGS.items():
        catalogue[key] = {
            "key": key,
            "name": config.get("name") or key.replace("_", " ").title(),
            "description": " ".join((config.get("description") or "").split()),
            "output": _output_kind(config),
            "source_url": (config.get("source") or {}).get("url", ""),
            "probe_prompt": config.get("probe_prompt") or "",
            "judge_prompt": config.get("judge_prompt") or "",
        }
    return catalogue


def rubric(key: str) -> dict:
    """Catalogue entry for ``key``; unknown keys (e.g. removed upstream) get a stub."""
    return rubrics().get(key) or {
        "key": key, "name": key, "description": "", "output": "severity",
        "source_url": "", "probe_prompt": "", "judge_prompt": "",
    }


def rubric_choices() -> list[dict]:
    """Rubrics for pickers, with the output label added."""
    return [{**r, "output_label": OUTPUT_LABELS[r["output"]]} for r in rubrics().values()]


def _clean(text: str | None, default: str) -> str:
    """Stored prompt: blank when it matches the rubric's own prompt."""
    text = (text or "").strip()
    return "" if text == default.strip() else text


def save_version(judge: Judge, *, rubric: str, probe_prompt: str = "", judge_prompt: str = "",
                 note: str = "", user=None) -> tuple[JudgeVersion, bool]:
    """Save a new version of ``judge`` when the versioned content changed.

    Returns ``(version, created)``; unchanged content returns the latest version.
    Prompts identical to the rubric's own are stored blank (= "rubric default"),
    so switching rubric later doesn't carry stale copies along.
    """
    if rubric not in rubrics():
        raise ValueError(f"Unknown rubric “{rubric}”.")
    info = rubrics()[rubric]
    content = {
        "rubric": rubric,
        "probe_prompt": _clean(probe_prompt, info["probe_prompt"]),
        "judge_prompt": _clean(judge_prompt, info["judge_prompt"]),
    }
    with transaction.atomic():
        # Lock the judge row so two saves can't claim the same version number.
        Judge.objects.select_for_update().filter(pk=judge.pk).first()
        latest = judge.versions.order_by("-version").first()
        if latest is not None and latest.content() == content:
            return latest, False
        number = (judge.versions.aggregate(n=Max("version"))["n"] or 0) + 1
        version = JudgeVersion.objects.create(
            judge=judge, version=number, rubric=rubric,
            probe_prompt=content["probe_prompt"], judge_prompt=content["judge_prompt"],
            note=note.strip()[:250], created_by=user,
        )
    return version, True


def create_judge(*, project, name: str, rubric: str = "", description: str = "", probe_prompt: str = "",
                 judge_prompt: str = "", user=None, note: str = "") -> Judge:
    name = name.strip()
    if not name:
        raise ValueError("Judge name is required.")
    if Judge.objects.filter(project=project, name=name).exists():
        raise ValueError(f"A judge named “{name}” already exists.")
    with transaction.atomic():
        judge = Judge.objects.create(project=project, name=name[:250], description=description.strip(), created_by=user)
        save_version(judge, rubric=rubric, probe_prompt=probe_prompt, judge_prompt=judge_prompt,
                     note=note or "Created", user=user)
    return judge


def unique_name(project, base: str) -> str:
    """``base``, or ``base (2)``, ``base (3)``… when taken."""
    taken = set(Judge.objects.filter(project=project, name__startswith=base).values_list("name", flat=True))
    if base not in taken:
        return base
    n = 2
    while f"{base} ({n})" in taken:
        n += 1
    return f"{base} ({n})"


def clone_judge(version: JudgeVersion, *, name: str = "", user=None) -> Judge:
    """A new judge starting from ``version`` (its own history starts at v1)."""
    src = version.judge
    return create_judge(
        project=src.project,
        name=name.strip() or unique_name(src.project, f"{src.name} copy"),
        description=src.description,
        rubric=version.rubric,
        probe_prompt=version.probe_prompt or rubric(version.rubric)["probe_prompt"],
        judge_prompt=version.judge_prompt or rubric(version.rubric)["judge_prompt"],
        note=f"Cloned from {version.label}",
        user=user,
    )


def ensure_starter_judges(project, user=None) -> list[str]:
    """Create one judge per starter rubric (idempotent by name)."""
    made = []
    for key in STARTER_RUBRICS:
        if key not in rubrics():
            continue
        name = rubrics()[key]["name"].removesuffix(" Judge")
        if Judge.objects.filter(project=project, name=name).exists():
            continue
        create_judge(project=project, name=name, rubric=key,
                     description=rubrics()[key]["description"], user=user)
        made.append(name)
    return made


DEFAULT_JUDGE_NAME = "Safety"


def default_judge_version(project, user=None) -> JudgeVersion:
    """The judge used when none is picked (API, quick runs): the Safety judge's
    latest version, created from SimpleAudit's safety rubric if missing."""
    judge = Judge.objects.filter(project=project, name=DEFAULT_JUDGE_NAME).first()
    if judge is None or judge.latest is None:
        judge = create_judge(project=project, name=unique_name(project, DEFAULT_JUDGE_NAME),
                             rubric="safety" if "safety" in rubrics() else "",
                             description=rubric("safety")["description"], user=user)
    return judge.latest


def judge_snapshot(version: JudgeVersion) -> dict:
    """What a run freezes about its judge, with prompts resolved to full text."""
    info = rubric(version.rubric)
    return {
        "judge_id": version.judge_id,
        "version_id": version.pk,
        "name": version.judge.name,
        "version": version.version,
        "rubric": version.rubric,
        "rubric_name": info["name"],
        "output": info["output"],
        # Blank = the rubric's own prompt; stored resolved so the run page can
        # show exactly what graded it, even after a SimpleAudit upgrade.
        "probe_prompt": version.probe_prompt or info["probe_prompt"],
        "judge_prompt": version.judge_prompt or info["judge_prompt"],
        "custom_probe_prompt": bool(version.probe_prompt),
        "custom_judge_prompt": bool(version.judge_prompt),
    }


def judge_usage_counts(project) -> dict[int, int]:
    """Runs per judge id."""
    from django.db.models import Count

    from audits.models import AuditRun

    rows = (
        AuditRun.objects.filter(project=project)
        .values("judge_version__judge_id")
        .annotate(n=Count("id"))
    )
    return {r["judge_version__judge_id"]: r["n"] for r in rows}
