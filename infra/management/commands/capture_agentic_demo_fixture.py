"""Export a sanitized, checksum-protected agentic demo fixture."""

from __future__ import annotations

import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from audits.agentic.demo_fixture import (
    export_run_fixture,
    fixture_checksum,
    relabel_models,
    sanitize_fixture,
    validate_fixture,
)


class Command(BaseCommand):
    help = "Validate and export a sanitized agentic demo fixture without model calls."

    def add_arguments(self, parser):
        source = parser.add_mutually_exclusive_group(required=True)
        source.add_argument("--input", help="JSON file containing a recorded run.")
        source.add_argument("--run", type=int, help="Completed DB AuditRun to export.")
        parser.add_argument("--output", required=True, help="Destination fixture JSON path.")
        parser.add_argument(
            "--require-recorded", action="store_true",
            help="Reject synthetic provenance; require an executed real run.",
        )
        parser.add_argument(
            "--relabel-models", metavar="LABEL",
            help="Relabel the auditor/judge models (e.g. \"Demo model (recorded)\") and drop "
                 "endpoint URLs, for a demo recorded on a model its users do not have.",
        )
        parser.add_argument(
            "--replace", action="append", default=[], metavar="OLD=NEW",
            help="With --relabel-models: rewrite OLD to NEW in every string (host names, model ids). Repeatable.",
        )

    def handle(self, *args, **options):
        input_path = Path(options["input"]) if options["input"] else None
        output_path = Path(options["output"])
        try:
            if options["run"] is not None:
                from audits.models import AuditRun

                run = AuditRun.objects.select_related("scenario_set_version", "judge_version").filter(
                    pk=options["run"]
                ).first()
                if run is None:
                    raise ValueError(f"AuditRun {options['run']} was not found")
                source = export_run_fixture(run)
            else:
                source = json.loads(input_path.read_text())
            validate_fixture(source, require_recorded=options["require_recorded"])
            if options["relabel_models"]:
                pairs = [item.split("=", 1) for item in options["replace"]]
                if any(len(pair) != 2 or not pair[0] for pair in pairs):
                    raise ValueError("--replace takes OLD=NEW")
                source = relabel_models(source, options["relabel_models"], replacements=dict(pairs))
            fixture = sanitize_fixture(source)
            sanitized_meta = dict(fixture.get("_meta") or {})
            sanitized_meta.pop("fixture_checksum", None)
            fixture["_meta"] = sanitized_meta
            sanitized_meta["fixture_checksum"] = fixture_checksum(fixture)
            validate_fixture(fixture, require_recorded=options["require_recorded"])
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise CommandError(f"Invalid agentic fixture: {exc}") from exc

        metadata = dict(fixture.get("_meta") or {})
        metadata.pop("fixture_checksum", None)
        metadata["sanitized"] = True
        fixture["_meta"] = metadata
        metadata["fixture_checksum"] = fixture_checksum(fixture)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(fixture, indent=2, sort_keys=True) + "\n")
        self.stdout.write(self.style.SUCCESS(f"Wrote sanitized fixture to {output_path}"))
