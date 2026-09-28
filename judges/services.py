"""Judges: SimpleAudit's judges and formats, versioning, cloning and run snapshots.

A judge version is a spec (``JudgeVersion.content()``): a SimpleAudit base
judge or a generic output format, the criteria, the probe prompt and format
options. ``library_judge`` turns a spec into what ``ModelAuditor(judge=...)``
takes, using SimpleAudit's ``customize_judge`` / ``build_judge``.
"""
from __future__ import annotations

import re
from functools import cache

from django.db import transaction
from django.db.models import Max

from judges.models import Judge, JudgeVersion

# What a judge's grade looks like, for pickers and result pages.
OUTPUT_LABELS = {
    "severity": "Severity",
    "score": "Score 1–10",
    "binary": "Yes / no",
    "checklist": "Checklist",
}

# The generic formats a judge can use with its own criteria (checklist only via
# SimpleAudit's checklist judge: its criteria are the checking procedure).
FORMATS = {
    "severity": {
        "label": "Severity ladder",
        "help": "The judge rates each conversation pass, low, medium, high or critical, "
                "and lists issues, what went well and recommendations.",
    },
    "score": {
        "label": "Score 1–10",
        "help": "The judge scores 1 to 10. Name dimensions to score each one; the score is "
                "their average. Low scores map onto the severity ladder.",
    },
    "binary": {
        "label": "Yes / no question",
        "help": "The judge answers one yes/no question about the assistant. A conversation "
                "passes when the answer is the one you choose; otherwise it gets the "
                "scenario's severity (high when the scenario has none).",
    },
}

MAX_DIMENSIONS = 10
DEFAULT_BASE = "default"   # SimpleAudit's unnamed default judge
# SimpleAudit's safety judge is its default judging behaviour ("Matches
# SimpleAudit's default judging behaviour"), so it stands in for the default.
DEFAULT_JUDGE_BASE = "safety"
DEFAULT_JUDGE_NAME = "Safety Judge (SimpleAudit Default)"


@cache
def bases() -> dict[str, dict]:
    """SimpleAudit's judges by key: name, description, output, source, criteria,
    probe prompt and format prompt. ``"default"`` is its unnamed default judge."""
    from simpleaudit.judges import DEFAULT_JUDGE, JUDGE_CONFIGS
    from simpleaudit.judges.compose import (
        SEVERITY_RESPONSE_SCHEMA as SEVERITY_FIELDS_SCHEMA,
    )

    catalogue = {}
    for key, config in [(DEFAULT_BASE, DEFAULT_JUDGE), *JUDGE_CONFIGS.items()]:
        catalogue[key] = {
            "key": key,
            "name": config.get("name") or key.replace("_", " ").title(),
            "description": " ".join((config.get("description") or "").split()),
            "output": config.get("output") or "severity",
            "source_url": (config.get("source") or {}).get("url", ""),
            "criteria": (config.get("criteria") or "").strip(),
            "probe_prompt": (config.get("probe_prompt") or "").strip(),
            "format_prompt": (config.get("format_prompt") or "").strip(),
            "fields": list((config.get("response_schema") or SEVERITY_FIELDS_SCHEMA)["properties"]),
        }
        catalogue[key].update(_generic_equivalent(config, SEVERITY_FIELDS_SCHEMA))
    return catalogue


def _generic_equivalent(config: dict, severity_schema: dict) -> dict:
    """How a SimpleAudit judge maps onto the generic formats, for cloning and
    for switching "Start from" to own criteria:

    - ``equivalent``: the generic format that grades the same way (same fields,
      no post-processing), or "" when the judge's output has more.
    - ``dimensions``: its 1–10 sub-scores, as score dimension names.
    - ``own_criteria``: its criteria for use with the equivalent format.
    """
    props = (config.get("response_schema") or severity_schema)["properties"]
    output = config.get("output") or "severity"
    subscores = [k for k, v in props.items() if k not in ("score", "feedback") and v.get("type") == "integer"]
    equivalent = ""
    if not config.get("postprocess"):
        if output == "severity" and list(props) == list(severity_schema["properties"]):
            equivalent = "severity"
        elif output == "score" and set(props) == {"score", "feedback", *subscores}:
            equivalent = "score"
    # The closing section the generic format supplies itself (the ladder, or
    # how the score is computed) is left out, so it isn't said twice.
    criteria = (config.get("criteria") or "").strip()
    header = {"severity": "SEVERITY LEVELS:", "score": "OVERALL SCORE:"}.get(equivalent)
    head, marker, tail = criteria.rpartition(header) if header else ("", "", "")
    if marker and head.strip() and not re.search(r"^[A-Z][A-Z /()-]+:\s*$", tail, re.MULTILINE):
        criteria = head.strip()
    return {
        "equivalent": equivalent,
        "dimensions": [k.replace("_", " ").capitalize() for k in subscores],
        "own_criteria": criteria,
    }


def base(key: str) -> dict:
    """Catalogue entry for ``key``; unknown keys (e.g. removed upstream) get a stub."""
    return bases().get(key) or {
        "key": key, "name": key, "description": "", "output": "severity", "source_url": "",
        "criteria": "", "probe_prompt": "", "format_prompt": "", "fields": [],
    }


def base_choices(current: str | None = None) -> list[dict]:
    """SimpleAudit's judges for pickers, with the output label added.

    The unnamed default is offered only when ``current`` already uses it
    (older versions): the Safety Judge is SimpleAudit's default.
    """
    return [
        {**b, "output_label": OUTPUT_LABELS[b["output"]]}
        for key, b in bases().items()
        if key != DEFAULT_BASE or current == DEFAULT_BASE
    ]


def default_probe_prompt() -> str:
    from simpleaudit.judges.default import DEFAULT_PROBE_PROMPT

    return DEFAULT_PROBE_PROMPT.strip()


def _clean_options(output: str, options: dict | None) -> dict:
    options = options or {}
    if output == "score":
        dims = options.get("dimensions") or []
        if isinstance(dims, str):
            dims = dims.replace(",", "\n").splitlines()
        return {"dimensions": [d.strip() for d in dims if d and d.strip()]}
    if output == "binary":
        pass_when = options.get("pass_when", True)
        if isinstance(pass_when, str):
            pass_when = pass_when.strip().lower() in ("true", "yes", "1")
        return {"question": (options.get("question") or "").strip(), "pass_when": bool(pass_when)}
    return {}


def make_spec(*, base: str = "", output: str = "", criteria: str = "", probe_prompt: str = "",
              options: dict | None = None) -> dict:
    """A validated, normalised spec. Text equal to the base's (or SimpleAudit's
    default probe) is stored blank, so it follows the base and switching base
    later doesn't carry stale copies along. Raises ``ValueError``."""
    criteria, probe_prompt = (criteria or "").strip(), (probe_prompt or "").strip()
    if base:
        if base not in bases():
            raise ValueError(f"Unknown SimpleAudit judge “{base}”.")
        info = bases()[base]
        output, options = info["output"], {}
        if criteria == info["criteria"]:
            criteria = ""
        default_probe = info["probe_prompt"]
    else:
        if output not in FORMATS:
            raise ValueError("Pick an output format.")
        if not criteria:
            raise ValueError("Write the criteria: what the judge should evaluate.")
        options = _clean_options(output, options)
        if len(options.get("dimensions", [])) > MAX_DIMENSIONS:
            raise ValueError(f"Use at most {MAX_DIMENSIONS} score dimensions.")
        default_probe = default_probe_prompt()
    if probe_prompt == default_probe:
        probe_prompt = ""
    spec = {"base": base, "output": output, "criteria": criteria, "probe_prompt": probe_prompt, "options": options}
    library_judge(spec)   # SimpleAudit's own checks: dimension names, the question, ...
    return spec


def library_judge(spec: dict):
    """What ``ModelAuditor(judge=...)`` takes for ``spec``.

    An unedited SimpleAudit judge is passed by name (None for the unnamed
    default), exactly as the library runs it. Edited criteria keep the base's
    format (``customize_judge``); a generic format is built from the criteria
    (``build_judge``). The probe prompt is passed separately.
    """
    from simpleaudit.judges import DEFAULT_JUDGE, build_judge, customize_judge

    key, criteria = spec.get("base") or "", spec.get("criteria") or ""
    if key and not criteria:
        return None if key == DEFAULT_BASE else key
    if key:
        return customize_judge(DEFAULT_JUDGE if key == DEFAULT_BASE else key, criteria=criteria)
    options = spec.get("options") or {}
    return build_judge(
        spec["output"], criteria,
        dimensions=options.get("dimensions") or (),
        question=options.get("question"),
        pass_when=options.get("pass_when", True),
    )


def resolve(spec: dict) -> dict:
    """The full texts ``spec`` grades with: criteria, format prompt, the whole
    judge prompt and the probe prompt, plus labels for display."""
    from simpleaudit.judges import DEFAULT_JUDGE, get_judge

    key = spec.get("base") or ""
    info = base(key) if key else None
    try:
        judge = library_judge(spec)
        # The unnamed default shows SimpleAudit's representative prompt.
        config = get_judge(DEFAULT_JUDGE if judge is None else judge)
    except ValueError:   # base removed upstream, or a spec SimpleAudit rejects
        config = {}
    output = spec.get("output") or "severity"
    return {
        "base_name": info["name"] if info else "",
        "output": output,
        "output_label": OUTPUT_LABELS.get(output, output),
        "criteria": (spec.get("criteria") or (info["criteria"] if info else "")).strip(),
        "format_prompt": (config.get("format_prompt") or "").strip(),
        "judge_prompt": (config.get("judge_prompt") or "").strip(),
        "probe_prompt": spec.get("probe_prompt") or (info["probe_prompt"] if info else default_probe_prompt()),
        "custom_criteria": bool(key and spec.get("criteria")),
        "custom_probe_prompt": bool(spec.get("probe_prompt")),
    }


def decorate(version: JudgeVersion) -> JudgeVersion:
    """Attach display attributes: ``resolved`` (``resolve``: full texts and
    labels), ``base_name``, ``output_label`` and a one-line ``summary``."""
    info = version.resolved = resolve(version.content())
    version.base_name, version.output_label = info["base_name"], info["output_label"]
    if version.base:
        edited = [label for flag, label in ((info["custom_criteria"], "criteria"),
                                            (info["custom_probe_prompt"], "probe")) if flag]
        version.summary = f"{info['base_name']} · {info['output_label']}" + (f" · edited {' + '.join(edited)}" if edited else "")
    else:
        detail = ""
        if version.output == "score" and version.options.get("dimensions"):
            detail = f" ({', '.join(version.options['dimensions'])})"
        version.summary = f"Own criteria · {info['output_label']}{detail}"
    return version


def form_start(version: JudgeVersion | None = None, *, base_key: str = "", output: str = "") -> str:
    """The value of the judge form's "Start from" picker: a base key, or ``format:<output>``."""
    if version is not None:
        base_key, output = version.base, version.output
    return base_key if base_key else f"format:{output or 'severity'}"


def parse_start(value: str) -> dict:
    """``make_spec`` base / output from the "Start from" value."""
    value = (value or "").strip()
    if value.startswith("format:"):
        return {"base": "", "output": value.removeprefix("format:")}
    return {"base": value, "output": ""}


def save_version(judge: Judge, *, note: str = "", user=None, **fields) -> tuple[JudgeVersion, bool]:
    """Save a new version of ``judge`` when the versioned content changed.

    ``fields`` are ``make_spec`` arguments. Returns ``(version, created)``;
    unchanged content returns the latest version.
    """
    content = make_spec(**fields)
    with transaction.atomic():
        # Lock the judge row so two saves can't claim the same version number.
        Judge.objects.select_for_update().filter(pk=judge.pk).first()
        latest = judge.versions.order_by("-version").first()
        if latest is not None and latest.content() == content:
            return latest, False
        number = (judge.versions.aggregate(n=Max("version"))["n"] or 0) + 1
        version = JudgeVersion.objects.create(
            judge=judge, version=number, **content, note=note.strip()[:250], created_by=user,
        )
    return version, True


def create_judge(*, project, name: str, description: str = "", user=None, note: str = "", **fields) -> Judge:
    """A new judge with its v1; ``fields`` are ``make_spec`` arguments
    (default: SimpleAudit's safety judge)."""
    name = name.strip()
    if not name:
        raise ValueError("Judge name is required.")
    if Judge.objects.filter(project=project, name=name).exists():
        raise ValueError(f"A judge named “{name}” already exists.")
    if not fields.get("base") and not fields.get("output"):
        fields["base"] = DEFAULT_JUDGE_BASE
    with transaction.atomic():
        judge = Judge.objects.create(project=project, name=name[:250], description=description.strip(), created_by=user)
        save_version(judge, note=note or "Created", user=user, **fields)
    return judge


def unique_name(project, name: str) -> str:
    """``name``, or ``name (2)``, ``name (3)``… when taken."""
    taken = set(Judge.objects.filter(project=project, name__startswith=name).values_list("name", flat=True))
    if name not in taken:
        return name
    n = 2
    while f"{name} ({n})" in taken:
        n += 1
    return f"{name} ({n})"


def delete_version(version: JudgeVersion) -> None:
    """Delete a version no run or monitor uses; the judge's other versions keep
    their numbers. The only version can't be deleted (delete the judge).
    Raises ``ValueError`` with the reason otherwise."""
    from django.db.models import RestrictedError

    if version.audit_runs.exists() or version.monitors.exists():
        raise ValueError(f"{version.label} is used by runs or monitors, so it is kept for reproducibility.")
    if version.judge.versions.count() == 1:
        raise ValueError(f"{version.label} is the judge's only version: delete the judge instead.")
    try:
        version.delete()
    except RestrictedError as e:   # a run started in the meantime
        raise ValueError(f"{version.label} is in use, so it is kept.") from e


def starter_name(key: str) -> str:
    return DEFAULT_JUDGE_NAME if key == DEFAULT_JUDGE_BASE else bases()[key]["name"]


def ensure_starter_judges(project, user=None) -> list[str]:
    """Mirror SimpleAudit's named judges, with the library's names and
    descriptions (the safety judge marked as the default). Idempotent: a base
    some workspace judge already starts from is skipped, and so is the default
    judge when it exists by name."""
    made = []
    for key, info in bases().items():
        if key == DEFAULT_BASE or JudgeVersion.objects.filter(judge__project=project, base=key).exists():
            continue
        name = starter_name(key)
        if key == DEFAULT_JUDGE_BASE and Judge.objects.filter(project=project, name=name).exists():
            continue
        name = unique_name(project, name)
        create_judge(project=project, name=name, base=key, description=info["description"], user=user)
        made.append(name)
    return made


def default_judge(project):
    """The workspace's default judge: "Safety Judge (SimpleAudit Default)", else
    the first judge based on SimpleAudit's safety judge. Pre-ticked on New Experiment and used
    when no judge is picked (API, demo runs)."""
    judge = Judge.objects.filter(project=project, name=DEFAULT_JUDGE_NAME).first()
    if judge is None:
        version = JudgeVersion.objects.filter(judge__project=project, base=DEFAULT_JUDGE_BASE).select_related(
            "judge").order_by("judge__created_at").first()
        judge = version.judge if version else None
    return judge


def default_judge_version(project, user=None) -> JudgeVersion:
    """Latest version of the default judge, created if the workspace has none."""
    judge = default_judge(project)
    if judge is None:
        judge = create_judge(project=project, name=unique_name(project, DEFAULT_JUDGE_NAME), base=DEFAULT_JUDGE_BASE,
                             description=base(DEFAULT_JUDGE_BASE)["description"], user=user)
    return judge.latest


def judge_snapshot(version: JudgeVersion) -> dict:
    """What a run freezes about its judge: the spec the engine builds the
    SimpleAudit judge from, and the texts it resolves to, so the run page can
    show exactly what graded it."""
    return {
        "judge_id": version.judge_id,
        "version_id": version.pk,
        "name": version.judge.name,
        "version": version.version,
        "base": version.base,
        "spec": version.content(),
        **resolve(version.content()),
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
