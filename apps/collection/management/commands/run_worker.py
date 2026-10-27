from django.conf import settings
from django.core.management.base import BaseCommand

from apps.collection.worker import Worker, run_forever


class Command(BaseCommand):
    help = ("Run the background worker: scheduled feeds, 'Run in background' requests, mailbox polling "
            "and health checks. Keep it running in its own terminal (Ctrl+C to stop).")

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true",
                            help="Do one pass (runs due feeds inline) and exit; useful with Windows Task Scheduler")
        parser.add_argument("--threads", type=int, default=None, help="Parallel feed runs (default EWCE_WORKER_THREADS)")
        parser.add_argument("--tick", type=int, default=30, help="Seconds between checks for due feeds")

    def handle(self, *args, **options):
        if options["once"]:
            worker = Worker(inline=True, threads=1)
            dispatched = worker.tick()
            worker.shutdown()
            self.stdout.write(self.style.SUCCESS(f"One pass done; ran {len(dispatched)} feed(s): {dispatched}"))
            return
        worker = Worker(threads=options["threads"])
        self.stdout.write(self.style.SUCCESS(
            f"{settings.EWCE_PRODUCT_NAME} worker {worker.name} started with {worker.threads} thread(s); checking every {options['tick']} s. "
            "Press Ctrl+C to stop."))
        run_forever(worker, tick_seconds=options["tick"])
        self.stdout.write("Worker stopped.")
