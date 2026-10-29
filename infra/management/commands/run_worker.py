"""Start a SimpleAudit Hatchet worker pool.

Usage:
    python manage.py run_worker [--pool cpu]

The worker connects to the external Hatchet server (HATCHET_SERVER_URL /
HATCHET_GRPC_URL) and executes audit scenario/finalize tasks. It runs in its own
process, separate from the web/API server, which never executes model calls.
"""

from django.conf import settings
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Run a SimpleAudit Hatchet worker pool."

    def add_arguments(self, parser):
        parser.add_argument(
            "--pool",
            default=settings.WORKER_POOL,
            help="Worker pool label (cpu/gpu). Defaults to WORKER_POOL env.",
        )

    def handle(self, *args, **options):
        # Allow the --pool flag to override the env-derived setting for this process.
        settings.WORKER_POOL = options["pool"]
        self.stdout.write(self.style.NOTICE(f"Starting SimpleAudit worker (pool={options['pool']})..."))
        self.stdout.write(self.style.NOTICE(f"Hatchet server: {settings.HATCHET_SERVER_URL}"))

        from chat.sync import ensure_chat_functions
        from infra.worker import start_worker

        # Bundled Open WebUI functions follow the code: refresh them on every worker start.
        for line in ensure_chat_functions():
            self.stdout.write(self.style.NOTICE(line))

        try:
            start_worker()
        except KeyboardInterrupt:
            self.stdout.write(self.style.WARNING("Worker stopped by interrupt."))
