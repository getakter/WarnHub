"""The background worker behind `manage.py run_worker` (no Redis/Celery: the database is the queue).

Every tick it finds feeds that are due (active, scheduled, next_run_at reached, inside their active
hours) or that someone asked to 'Run in background', advances next_run_at, and runs them in a thread
pool. The per-feed database lock in run_feed() keeps runs from overlapping even with several workers.
"""
import logging
import os
import socket
import threading
from concurrent.futures import ThreadPoolExecutor

from django.conf import settings
from django.db import close_old_connections
from django.db.models import Q
from django.utils import timezone

from apps.plugins.registry import PluginNotFound, registry
from apps.sources.models import Feed

from .health import refresh_all_health
from .models import WorkerHeartbeat
from .pipeline import run_feed
from .scheduling import next_run_for_feed, within_active_hours

logger = logging.getLogger("ewce.worker")


def _schedulable(feed):
    try:
        return getattr(registry.get("fetcher", feed.method), "schedulable", True)
    except PluginNotFound:
        return True  # let run_feed record the error on the feed


class Worker:
    def __init__(self, threads=None, inline=False, name=None):
        self.inline = inline
        self.name = name or f"{socket.gethostname()}:{os.getpid()}"
        self.threads = threads or settings.EWCE_WORKER_THREADS
        self.executor = None if inline else ThreadPoolExecutor(max_workers=self.threads, thread_name_prefix="ewce-run")
        self._running = set()
        self._lock = threading.Lock()
        self.started_at = timezone.now()

    def _fresh_db(self):
        """Recycle stale DB connections between jobs (threaded mode only: inline runs share the caller's)."""
        if not self.inline:
            close_old_connections()

    # -- dispatching ------------------------------------------------------------------
    def due_feeds(self, now):
        scheduled = Q(is_active=True, source__is_active=True, next_run_at__lte=now) & ~Q(schedule_type="manual")
        return Feed.objects.filter(scheduled | Q(run_requested_at__isnull=False)).select_related("source")

    def dispatch(self, now=None):
        """Start every due feed. Returns the ids dispatched in this tick."""
        now = now or timezone.now()
        dispatched = []
        for feed in self.due_feeds(now):
            requested = feed.run_requested_at is not None
            updates = {"run_requested_at": None}
            if feed.is_active and feed.schedule_type != "manual":
                updates["next_run_at"] = next_run_for_feed(feed, after=now)
            Feed.objects.filter(pk=feed.pk).update(**updates)
            if not _schedulable(feed):
                logger.info("feed=%s method=%s cannot be scheduled; skipped", feed.pk, feed.method)
                continue
            if not requested and not within_active_hours(feed.active_hours_start, feed.active_hours_end, now):
                logger.info("feed=%s outside active hours; next run %s", feed.pk, updates.get("next_run_at"))
                continue
            with self._lock:
                if feed.pk in self._running:
                    continue
                self._running.add(feed.pk)
            dispatched.append(feed.pk)
            trigger = "manual" if requested else "schedule"
            if self.inline:
                self._run(feed.pk, trigger)
            else:
                self.executor.submit(self._run, feed.pk, trigger)
        return dispatched

    def _run(self, feed_id, trigger):
        self._fresh_db()
        try:
            feed = Feed.objects.select_related("source").get(pk=feed_id)
            ctx = run_feed(feed, trigger=trigger, worker=self.name)
            logger.info("feed=%s run=%s status=%s stats=%s", feed_id, getattr(ctx.run, "pk", None), ctx.status,
                        dict(ctx.stats))
        except Exception:  # noqa: BLE001 - the worker must keep going
            logger.exception("feed=%s run crashed", feed_id)
        finally:
            with self._lock:
                self._running.discard(feed_id)
            self._fresh_db()

    # -- housekeeping -----------------------------------------------------------------
    def heartbeat(self):
        self._fresh_db()
        with self._lock:
            running = sorted(self._running)
        WorkerHeartbeat.objects.update_or_create(
            name=self.name,
            defaults={"hostname": socket.gethostname(), "pid": os.getpid(), "started_at": self.started_at,
                      "last_seen_at": timezone.now(), "running_feeds": running,
                      "info": {"threads": self.threads}},
        )

    def poll_mailboxes(self):
        try:
            from apps.mailbox.service import poll_due_mailboxes
        except ImportError:
            return []
        self._fresh_db()
        return poll_due_mailboxes(worker=self.name)

    def check_health(self):
        self._fresh_db()
        return refresh_all_health()

    def tick(self):
        """One full pass (used by --once and by tests)."""
        self.heartbeat()
        dispatched = self.dispatch()
        self.poll_mailboxes()
        self.check_health()
        return dispatched

    def shutdown(self):
        if self.executor is not None:
            self.executor.shutdown(wait=True, cancel_futures=False)
        WorkerHeartbeat.objects.filter(name=self.name).delete()


def _safe(job):
    def wrapper():
        try:
            job()
        except Exception:  # noqa: BLE001 - keep the scheduler alive
            logger.exception("worker job %s failed", job.__name__)
    wrapper.__name__ = job.__name__
    return wrapper


def run_forever(worker, tick_seconds=30):
    from apscheduler.schedulers.blocking import BlockingScheduler

    scheduler = BlockingScheduler(timezone=settings.TIME_ZONE)
    defaults = {"max_instances": 1, "coalesce": True, "misfire_grace_time": 60}
    scheduler.add_job(_safe(worker.dispatch), "interval", seconds=tick_seconds, id="dispatch", next_run_time=timezone.now(), **defaults)
    scheduler.add_job(_safe(worker.poll_mailboxes), "interval", seconds=60, id="mailboxes", next_run_time=timezone.now(), **defaults)
    scheduler.add_job(_safe(worker.heartbeat), "interval", seconds=60, id="heartbeat", next_run_time=timezone.now(), **defaults)
    scheduler.add_job(_safe(worker.check_health), "interval", minutes=5, id="health", next_run_time=timezone.now(), **defaults)
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        worker.shutdown()
