"""Generate a standalone SimpleAudit script from a run's frozen inputs.

Two entry points feed one renderer:

- ``generate_run_script(run)`` — re-run an existing ``AuditRun`` from its
  frozen snapshots (the "Re-run script" button on the run page);
- ``generate_run_script_from_spec(run_spec)`` — a script for a planned run
  (a ``spec_to_run`` item) before it launches, freezing the same inputs
  ``create_audit_run`` would.

The generated script reproduces the run outside the platform using only the
``simpleaudit`` library: the exact scenarios (frozen revisions), the three
models (target / auditor / judge), the judge spec, and the generation
settings. It is deterministic — the same inputs always yield the same script
— so it can be committed next to results for reproducibility.

Secrets are never inlined. Each model's API key is read at run time from the
environment variable named by its connection's ``secret_reference``; when a
connection stores the key directly instead, the script prints a reminder to
export that variable before running.
"""

from __future__ import annotations

import json

from audits.models import AuditRun
from scenarios.models import ScenarioSetVersionItem


def _py(value) -> str:
    """A Python literal for use inside the generated script."""
    return json.dumps(value, indent=4, ensure_ascii=False)


def _endpoint_block(snap: dict, *, prefix: str, indent: str = "") -> list:
    """Lines for one endpoint from its frozen snapshot.

    Returns a list of items: plain strings are code lines; a
    ``("#", text)`` item is a comment line (rendered without a trailing comma).

    ``prefix`` is "target"/"auditor"/"judge" for constructor keyword
    arguments (ModelAuditor, or AuditExperiment's non-target roles). An empty
    prefix emits plain dict entries (quoted keys) for the target inside
    AuditExperiment's ``models`` list. ``indent`` is the base indentation for
    every emitted line.

    When the connection stores its API key directly in Studio (no
    ``secret_reference``), no live api_key line is emitted — instead a
    commented placeholder sits exactly where the line belongs, so the user can
    uncomment and fill it in.
    """
    provider = (snap.get("provider") or "").strip().lower()
    base_url = snap.get("base_url") or None
    # Mirror infra.engine._normalize_provider: an unrecognised label with a
    # base URL is an OpenAI-compatible endpoint.
    known = {"openai", "anthropic", "grok", "ollama", "vllm"}
    if provider not in known:
        provider = "openai" if base_url else (provider or "openai")
    ref = (snap.get("secret_reference") or "").strip()
    env = f"**{(prefix or 'TARGET').upper()}_API_KEY**"

    if prefix:
        p = f"{prefix}_"
        items = [
            f'{indent}{p}model="{snap.get("model_id")}"',
            f'{indent}{p}provider="{provider}"',
        ]
        if base_url:
            items.append(f'{indent}{p}base_url="{base_url}"')
        if ref:
            items.append(f'{indent}{p}api_key=os.environ["{ref}"]')
        else:
            items.append(("#", f'{indent}# {p}api_key=os.environ["{env}"]  # uncomment & export {env}'))
        return items
    # Plain dict entries for the models list.
    items = [
        f'{indent}"model": "{snap.get("model_id")}"',
        f'{indent}"provider": "{provider}"',
    ]
    if base_url:
        items.append(f'{indent}"base_url": "{base_url}"')
    if ref:
        items.append(f'{indent}"api_key": os.environ["{ref}"]')
    else:
        items.append(("#", f'{indent}# "api_key": os.environ["{env}"],  # uncomment & export {env}'))
    return items


def _scenario_dicts(version) -> list[dict]:
    """The scenario content of a set version, in set order."""
    items = (
        ScenarioSetVersionItem.objects.filter(version=version)
        .select_related("scenario", "revision")
        .order_by("position")
    )
    out = []
    for item in items:
        d = {
            "name": item.scenario.key,
            "description": item.revision.description,
        }
        if item.revision.test_prompt:
            d["test_prompt"] = item.revision.test_prompt
        if item.revision.expected_behavior:
            d["expected_behavior"] = item.revision.expected_behavior
        out.append(d)
    return out


def _render(header: str, *, version, target: dict, auditor: dict, judge_snap: dict, gen: dict) -> str:
    """Emit the script from a normalized context (frozen snapshots + settings)."""
    grading = judge_snap.get("judge") or {}
    spec = grading.get("spec") or {}

    n_reps = int(gen.get("n_repetitions") or 1)
    max_turns = int(gen.get("max_turns") or 5)
    language = gen.get("language") or "English"
    system_prompt = gen.get("system_prompt") or None

    imports = ["import os", "from simpleaudit import ModelAuditor"]
    body_parts: list[str] = []

    # --- Scenarios ---------------------------------------------------------
    scenarios = _scenario_dicts(version)
    set_name = version.scenario_set.name
    set_version = version.version
    body_parts.append(
        f"# Scenarios: \"{set_name}\" v{set_version} ({len(scenarios)} scenarios, frozen)\n"
        f"scenarios = {_py(scenarios)}"
    )

    # --- Judge -------------------------------------------------------------
    judge_lines = []
    base = spec.get("base") or ""
    criteria = spec.get("criteria") or ""
    probe_prompt = spec.get("probe_prompt") or None
    if base and not criteria:
        # An unedited SimpleAudit judge: pass by name (None = unnamed default).
        if base != "default":
            judge_lines.append(f'judge="{base}"')
    elif base:
        # Edited criteria on top of a library judge.
        judge_lines.append(
            "judge=customize_judge(\n"
            f"    {json.dumps(base)},\n"
            f"    criteria={_py(criteria)},\n"
            ")"
        )
        imports.append("from simpleaudit.judges import customize_judge")
    else:
        options = spec.get("options") or {}
        judge_lines.append(
            "judge=build_judge(\n"
            f"    {json.dumps(spec.get('output'))},\n"
            f"    criteria={_py(criteria)},\n"
            f"    dimensions={_py(options.get('dimensions') or ())},\n"
            f"    question={_py(options.get('question'))},\n"
            f"    pass_when={_py(options.get('pass_when', True))},\n"
            ")"
        )
        imports.append("from simpleaudit.judges import build_judge")
    if probe_prompt:
        judge_lines.append(f"probe_prompt={_py(probe_prompt)}")

    # --- Assemble the constructor ------------------------------------------
    # Every line is an item: a plain string renders with a trailing comma, a
    # ("comment", text) item renders as-is (no comma). Endpoint blocks emit
    # commented api_key placeholders where the key is stored directly in Studio.
    def render(items) -> str:
        return "".join(
            f"{it[1]}\n" if isinstance(it, tuple) else f"{it},\n" for it in items
        )

    IND = "    "
    judge_items = [(IND + line) for line in judge_lines]
    extra = []
    if system_prompt:
        extra.append(IND + f"system_prompt={_py(system_prompt)}")
    if max_turns != 5:
        extra.append(IND + f"max_turns={max_turns}")

    if n_reps > 1:
        # AuditExperiment takes the target as a models list (plain keys);
        # auditor and judge stay constructor kwargs. The dict opens on its own
        # line: "models=[{" would tokenize as a set display.
        model_entry = (
            "[\n    {\n"
            + render(_endpoint_block(target, prefix="", indent="        "))
            + "    }\n]"
        )
        items = [IND + f"models={model_entry}"]
        items += _endpoint_block(auditor, prefix="auditor", indent=IND)
        items += _endpoint_block(judge_snap, prefix="judge", indent=IND)
        items += judge_items + extra + [IND + f"n_repetitions={n_reps}", IND + "show_progress=True"]
        imports[imports.index("from simpleaudit import ModelAuditor")] = (
            "from simpleaudit import AuditExperiment"
        )
        var_name, cls = "experiment", "AuditExperiment"
        run_call = f"results = experiment.run(scenarios, language={_py(language)})"
    else:
        items = _endpoint_block(target, prefix="target", indent=IND)
        items += _endpoint_block(auditor, prefix="auditor", indent=IND)
        items += _endpoint_block(judge_snap, prefix="judge", indent=IND)
        items += judge_items + extra + [IND + "show_progress=True"]
        var_name, cls = "auditor", "ModelAuditor"
        run_call = f"results = auditor.run(scenarios, language={_py(language)})"

    body_parts.append(f"{var_name} = {cls}(\n{render(items)})")

    body_parts.append(
        f"{run_call}\n"
        "results.summary()\n"
        'results.save("results.json")'
    )

    script = header + "\n".join(dict.fromkeys(imports)) + "\n\n\n" + "\n\n\n".join(body_parts) + "\n"
    return script


def generate_run_script(run: AuditRun) -> str:
    """Build the standalone script that re-runs ``run`` with plain simpleaudit."""
    header = (
        f'"""Re-run of Studio run #{run.id} "{run.name}".\n'
        "\n"
        "Generated by SimpleAudit Studio — edit freely.\n"
        "Requires: pip install simpleaudit\n"
        "Set the API-key environment variables noted below, then:\n"
        "    python this_file.py\n"
        '"""\n'
    )
    return _render(
        header,
        version=run.scenario_set_version,
        target=run.target_config_snapshot or {},
        auditor=run.auditor_config_snapshot or {},
        judge_snap=run.judge_config_snapshot or {},
        gen=run.generation_parameters_snapshot or {},
    )


def generate_run_script_from_spec(run_spec: dict) -> str:
    """Script for a planned run (a ``spec_to_run`` item) before it is launched.

    Freezes the same inputs ``create_audit_run`` would, so the script matches
    the run that launching the spec produces.
    """
    from audits.services import frozen_inputs

    name = run_spec.get("name") or "planned run"
    header = (
        f'"""SimpleAudit run script for "{name}" (planned, not yet launched).\n'
        "\n"
        "Generated by SimpleAudit Studio — edit freely.\n"
        "Requires: pip install simpleaudit\n"
        "Set the API-key environment variables noted below, then:\n"
        "    python this_file.py\n"
        '"""\n'
    )
    frozen = frozen_inputs(
        target_model=run_spec["target"],
        auditor_model=run_spec["auditor"],
        judge_model=run_spec["judge_model"],
        judge=run_spec["judge"],
        max_turns_override=run_spec["max_turns"],
        language_override=run_spec["language"],
        n_repetitions_override=run_spec["n_repetitions"],
        gen_config_override=run_spec["gen_config"],
    )
    return _render(
        header,
        version=run_spec["version"],
        target=frozen["target_config_snapshot"],
        auditor=frozen["auditor_config_snapshot"],
        judge_snap=frozen["judge_config_snapshot"],
        gen=frozen["generation_parameters_snapshot"],
    )


def generate_judge_script(judge_name: str, spec: dict) -> str:
    """A standalone snippet that builds one judge from its spec.

    ``spec`` is the shape ``JudgeVersion.content()`` returns (base / output /
    criteria / probe_prompt / options). Emits just the judge construction —
    no scenarios or models — so it can be dropped into any script. The same
    three shapes the run script handles: an unedited library judge by name,
    ``customize_judge`` for edited criteria on a base, and ``build_judge`` for
    own-criteria judges.
    """
    base = spec.get("base") or ""
    criteria = spec.get("criteria") or ""
    probe_prompt = spec.get("probe_prompt") or None
    imports = []
    if base and not criteria:
        # An unedited SimpleAudit judge: pass by name (None = unnamed default).
        if base != "default":
            judge_expr = f'"{base}"'
        else:
            judge_expr = "None"
    elif base:
        judge_expr = (
            "customize_judge(\n"
            f"    {json.dumps(base)},\n"
            f"    criteria={_py(criteria)},\n"
            ")"
        )
        imports.append("from simpleaudit.judges import customize_judge")
    else:
        options = spec.get("options") or {}
        judge_expr = (
            "build_judge(\n"
            f"    {json.dumps(spec.get('output'))},\n"
            f"    criteria={_py(criteria)},\n"
            f"    dimensions={_py(options.get('dimensions') or ())},\n"
            f"    question={_py(options.get('question'))},\n"
            f"    pass_when={_py(options.get('pass_when', True))},\n"
            ")"
        )
        imports.append("from simpleaudit.judges import build_judge")

    lines = [f"judge = {judge_expr}"]
    if probe_prompt:
        lines.append(f"probe_prompt = {_py(probe_prompt)}")

    header = (
        f'"""SimpleAudit judge "{judge_name}".\n'
        "\n"
        "Generated by SimpleAudit Studio — edit freely.\n"
        "Requires: pip install simpleaudit\n"
        '"""\n'
    )
    body = "\n".join(lines) + "\n\nprint(judge)\n"
    return header + "\n".join(dict.fromkeys(imports)) + "\n\n\n" + body
