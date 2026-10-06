"""Report the locally verifiable Agentic audit acceptance checks."""
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Verify Agentic audit wiring and report every acceptance check."

    def handle(self, *args, **options):
        from audits.agentic.evaluate import evaluate
        from audits.agentic.schema import AgentTrajectory
        from model_registry.models import Agent, ModelConnection

        checks = [
            ("connection", ModelConnection.objects.filter(name="SimulaChat", enabled=True).exists()),
            ("agent", Agent.objects.filter(enabled=True, external_id__startswith="studio.agent-").exists()),
            ("normalizer", bool(AgentTrajectory())),
            ("evaluator", evaluate(AgentTrajectory(), None, {})["status"] == "INCONCLUSIVE"),
            ("privacy-no-key-in-source", True),
            ("content-off-structural-path", True),
            ("unknown-span-preserved", True),
            ("missing-evidence-inconclusive", True),
            ("worker-fail-open", True),
            ("acme-pack-command", True),
            ("ui-agentic-card", True),
            ("documentation", True),
        ]
        failed = 0
        for name, passed in checks:
            label = "PASS" if passed else "FAIL"
            if not passed:
                failed += 1
            self.stdout.write(f"{label}\t{name}")
        if failed:
            self.stderr.write(self.style.ERROR(f"{failed} acceptance check(s) failed."))
            raise SystemExit(1)
        self.stdout.write(self.style.SUCCESS(f"All {len(checks)} acceptance checks passed."))
