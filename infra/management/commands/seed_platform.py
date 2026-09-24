"""Idempotent first-run seed: import SimpleAudit scenario packs + default model connections.

Run automatically by the web container after bootstrap_platform, or manually:
    python manage.py seed_platform [--project 1] [--packs safety rag health system_prompt]

Safe to run multiple times — skips anything already present.
"""
from __future__ import annotations

import hashlib
import json
import logging

from django.core.management.base import BaseCommand, CommandError

logger = logging.getLogger(__name__)

DEFAULT_PACKS = ["safety", "rag", "health", "system_prompt"]

# Default model connections to create if missing.
# (connection_name, provider, base_url, [(display_name, model_id), ...])
DEFAULT_MODELS = [
    (
        "OpenAI",
        "openai",
        "https://api.openai.com/v1",
        [
            ("GPT-4o", "gpt-4o"),
            ("GPT-4o Mini", "gpt-4o-mini"),
            ("GPT-4.1", "gpt-4.1"),
            ("GPT-4.1 Mini", "gpt-4.1-mini"),
            ("o3", "o3"),
            ("o3-mini", "o3-mini"),
        ],
    ),
]


class Command(BaseCommand):
    help = "Seed scenario packs and default model connections (idempotent)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--project", type=int, default=1,
            help="Project ID to seed (default: 1)",
        )
        parser.add_argument(
            "--packs", nargs="+", default=DEFAULT_PACKS,
            help=f"Scenario packs to import (default: {' '.join(DEFAULT_PACKS)})",
        )
        parser.add_argument(
            "--skip-models", action="store_true",
            help="Skip creating default model connections",
        )
        parser.add_argument(
            "--skip-packs", action="store_true",
            help="Skip importing scenario packs",
        )

    def handle(self, *args, **options):
        from accounts.models import Project
        from django.contrib.auth import get_user_model

        User = get_user_model()

        project_id = options["project"]
        try:
            project = Project.objects.get(id=project_id)
        except Project.DoesNotExist:
            raise CommandError(
                f"Project {project_id} not found. Run bootstrap_platform first."
            )

        user = User.objects.filter(is_superuser=True).first() or User.objects.first()
        if not user:
            raise CommandError("No user found. Run bootstrap_platform first.")

        self.stdout.write(f"Seeding project '{project.name}' (id={project_id}) as {user.username}")

        if not options["skip_packs"]:
            self._seed_scenario_packs(project, user, options["packs"])

        if not options["skip_models"]:
            self._seed_model_connections(project, user)

        self.stdout.write(self.style.SUCCESS("Seed complete."))

    def _seed_scenario_packs(self, project, user, packs: list[str]):
        from scenarios.models import (
            Scenario, ScenarioRevision, ScenarioSet,
            ScenarioSetVersion, ScenarioSetVersionItem,
        )
        from scenarios.services import publish_scenario_set_version

        try:
            from simpleaudit import get_scenarios, list_scenario_packs
        except ImportError:
            self.stderr.write(
                self.style.WARNING(
                    "simpleaudit package not installed — skipping scenario pack import."
                )
            )
            return

        available = set(list_scenario_packs())
        for pack_name in packs:
            if pack_name not in available:
                self.stderr.write(
                    self.style.WARNING(f"Pack '{pack_name}' not available. Skipping.")
                )
                continue

            set_name = f"SimpleAudit: {pack_name}"
            existing = ScenarioSet.objects.filter(project=project, name=set_name).first()
            if existing and existing.versions.exists():
                self.stdout.write(f"  ⏭ {set_name} already exists — skipping.")
                continue

            scenarios_data = get_scenarios(pack_name)
            if not scenarios_data:
                self.stdout.write(f"  ⏭ {set_name}: pack is empty — skipping.")
                continue

            self.stdout.write(f"  Importing {set_name} ({len(scenarios_data)} scenarios)...")

            # A set from an interrupted earlier run has no version; drop it and
            # re-import (scenarios are reused by key, so nothing is duplicated).
            if existing:
                existing.delete()

            scenario_set = ScenarioSet.objects.create(
                project=project,
                name=set_name,
                description=(
                    f"Imported from SimpleAudit built-in pack '{pack_name}'. "
                    f"{len(scenarios_data)} scenarios."
                ),
                created_by=user,
            )

            scenario_ids = []
            for i, sc_data in enumerate(scenarios_data):
                name = sc_data.get("name", f"Scenario {i + 1}")
                description = sc_data.get("description", "")
                expected_behavior = sc_data.get("expected_behavior", [])
                test_prompt = sc_data.get("test_prompt", "")
                category = sc_data.get("category", pack_name)
                tags = sc_data.get("metadata", {}).get("tags", [pack_name])

                key = f"simpleaudit_{pack_name}_{i:03d}"

                scenario, sc_created = Scenario.objects.get_or_create(
                    project=project,
                    key=key,
                    defaults={
                        "title": name,
                        "category": category,
                        "tags": tags,
                        "created_by": user,
                    },
                )

                content_hash = self._compute_hash(description, expected_behavior, test_prompt)
                revision, rev_created = ScenarioRevision.objects.get_or_create(
                    scenario=scenario,
                    revision=1,
                    defaults={
                        "description": description,
                        "expected_behavior": expected_behavior,
                        "test_prompt": test_prompt,
                        "content_hash": content_hash,
                        "created_by": user,
                    },
                )

                scenario_ids.append(scenario.id)

            version = publish_scenario_set_version(
                scenario_set=scenario_set,
                user=user,
                scenario_ids=scenario_ids,
            )

            self.stdout.write(
                self.style.SUCCESS(
                    f"    Created set id={scenario_set.id}, "
                    f"v{version.version}, {version.scenario_count} scenarios"
                )
            )

    def _seed_model_connections(self, project, user):
        from model_registry.models import ModelConnection, RegisteredModel, ModelEndpoint

        for conn_name, provider, base_url, models in DEFAULT_MODELS:
            conn, created = ModelConnection.objects.get_or_create(
                project=project,
                name=conn_name,
                defaults={
                    "provider": provider,
                    "base_url": base_url,
                    "enabled": True,
                    "created_by": user,
                },
            )
            if created:
                self.stdout.write(f"  ✓ Created model connection '{conn_name}'")
            else:
                self.stdout.write(f"  ⏭ Model connection '{conn_name}' already exists")

            for display_name, model_id in models:
                _, m_created = RegisteredModel.objects.get_or_create(
                    connection=conn,
                    project=project,
                    model_id=model_id,
                    defaults={
                        "display_name": display_name,
                        "enabled": True,
                        "default_parameters": {"temperature": 0.7, "max_tokens": 4096},
                    },
                )
                # Also create legacy ModelEndpoint for AuditRun FK compatibility
                _, ep_created = ModelEndpoint.objects.get_or_create(
                    project=project,
                    display_name=display_name,
                    defaults={
                        "provider": provider,
                        "base_url": base_url,
                        "model_id": model_id,
                        "enabled": True,
                        "created_by": user,
                    },
                )
                if m_created or ep_created:
                    self.stdout.write(f"    + {display_name} ({model_id})")

    @staticmethod
    def _compute_hash(description: str, expected_behavior: list, test_prompt: str) -> str:
        payload = json.dumps(
            {
                "description": description,
                "expected_behavior": expected_behavior,
                "test_prompt": test_prompt,
            },
            sort_keys=True,
            ensure_ascii=False,
        )
        return hashlib.sha256(payload.encode()).hexdigest()
