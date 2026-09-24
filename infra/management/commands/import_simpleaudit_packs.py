"""Import SimpleAudit built-in scenario packs as platform ScenarioSets.

Usage:
    python manage.py import_simpleaudit_packs [--packs safety rag health] [--project 1]

By default imports the core packs: safety, rag, health, system_prompt.
Creates ScenarioSet + Scenarios + ScenarioRevisions + publishes v1.
Idempotent: skips packs already imported (matched by key prefix).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging

from django.core.management.base import BaseCommand, CommandError

logger = logging.getLogger(__name__)

DEFAULT_PACKS = ["safety", "rag", "health", "system_prompt"]


class Command(BaseCommand):
    help = "Import SimpleAudit built-in scenario packs into the platform"

    def add_arguments(self, parser):
        parser.add_argument(
            "--packs", nargs="+", default=DEFAULT_PACKS,
            help=f"Packs to import (default: {' '.join(DEFAULT_PACKS)})",
        )
        parser.add_argument(
            "--project", type=int, default=1,
            help="Project ID (default: 1)",
        )
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Show what would be imported without writing",
        )

    def handle(self, *args, **options):
        from simpleaudit import get_scenarios, list_scenario_packs

        from accounts.models import Project
        from scenarios.models import (
            Scenario, ScenarioRevision, ScenarioSet,
            ScenarioSetVersion, ScenarioSetVersionItem,
        )
        from scenarios.services import publish_scenario_set_version

        project_id = options["project"]
        dry_run = options["dry_run"]
        requested_packs = options["packs"]

        # Validate project
        try:
            project = Project.objects.get(id=project_id)
        except Project.DoesNotExist:
            raise CommandError(f"Project {project_id} not found")

        # Validate packs exist in simpleaudit
        available = set(list_scenario_packs())
        for pack in requested_packs:
            if pack not in available:
                raise CommandError(
                    f"Pack '{pack}' not found. Available: {sorted(available)}"
                )

        self.stdout.write(f"Project: {project.name} (id={project_id})")
        self.stdout.write(f"Packs to import: {requested_packs}")
        self.stdout.write(f"Dry run: {dry_run}")
        self.stdout.write("")

        for pack_name in requested_packs:
            scenarios_data = get_scenarios(pack_name)
            pack_key = f"simpleaudit_{pack_name}"

            # Check if already imported
            existing_set = ScenarioSet.objects.filter(
                project=project, name=f"SimpleAudit: {pack_name}"
            ).first()
            if existing_set:
                self.stdout.write(
                    self.style.WARNING(
                        f"  SKIP {pack_name}: set already exists (id={existing_set.id})"
                    )
                )
                continue

            self.stdout.write(f"  Importing {pack_name} ({len(scenarios_data)} scenarios)...")

            if dry_run:
                for i, sc in enumerate(scenarios_data[:3]):
                    print(f"    [{i}] {sc.get('name', 'unnamed')}")
                if len(scenarios_data) > 3:
                    print(f"    ... and {len(scenarios_data) - 3} more")
                continue

            # Create ScenarioSet
            scenario_set = ScenarioSet.objects.create(
                project=project,
                name=f"SimpleAudit: {pack_name}",
                description=(
                    f"Imported from SimpleAudit built-in pack '{pack_name}'. "
                    f"{len(scenarios_data)} scenarios."
                ),
            )

            # Create Scenarios + Revisions
            scenario_ids = []
            for i, sc_data in enumerate(scenarios_data):
                name = sc_data.get("name", f"Scenario {i+1}")
                description = sc_data.get("description", "")
                expected_behavior = sc_data.get("expected_behavior", [])
                test_prompt = sc_data.get("test_prompt", "")
                category = sc_data.get("category", pack_name)
                tags = sc_data.get("metadata", {}).get("tags", [pack_name])

                # Generate stable key
                key = f"{pack_key}_{i:03d}"

                scenario = Scenario.objects.create(
                    project=project,
                    key=key,
                    title=name,
                    category=category,
                    tags=tags,
                )

                revision = ScenarioRevision.objects.create(
                    scenario=scenario,
                    revision=1,
                    description=description,
                    expected_behavior=expected_behavior,
                    test_prompt=test_prompt,
                    content_hash=self._compute_hash(description, expected_behavior, test_prompt),
                )

                scenario_ids.append(scenario.id)

            # Publish v1
            user = None
            from django.contrib.auth import get_user_model
            User = get_user_model()
            user = User.objects.first()

            version = publish_scenario_set_version(
                scenario_set=scenario_set,
                user=user,
                scenario_ids=scenario_ids,
            )

            self.stdout.write(
                self.style.SUCCESS(
                    f"    Created set id={scenario_set.id}, "
                    f"version v{version.version}, {version.scenario_count} scenarios"
                )
            )

        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS("Done."))

    @staticmethod
    def _compute_hash(description: str, expected_behavior: list, test_prompt: str) -> str:
        payload = json.dumps({
            "description": description,
            "expected_behavior": expected_behavior,
            "test_prompt": test_prompt,
        }, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(payload.encode()).hexdigest()
