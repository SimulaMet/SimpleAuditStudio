"""Export a sanitized, checksum-protected agentic demo fixture."""

from __future__ import annotations

import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from audits.agentic.demo_fixture import (
    export_run_fixture,
    fixture_checksum,
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
