"""Scheduling, the background worker and feed health."""
import datetime as dt
from datetime import timedelta

import pytest
from django.core.management import call_command
from django.urls import reverse
from django.utils import timezone

from apps.collection.health import evaluate, layout_change, refresh_all_health
from apps.collection.models import CollectedMessage, FetchRun, WorkerHeartbeat
from apps.collection.scheduling import compute_next_run, validate_cron, within_active_hours
from apps.collection.worker import Worker
from apps.sources.models import Feed

pytestmark = pytest.mark.django_db
DHAKA = dt.timezone(dt.timedelta(hours=6))


def _doc(url, text):
    return {"data": text.encode("utf-8"), "url": url, "content_type": "text/plain; charset=utf-8"}


@pytest.fixture
def scheduled(feed):
    feed.is_active = True
    feed.schedule_type = "interval"
    feed.interval_minutes = 60
    feed.save()
    return feed


# --- scheduling ----------------------------------------------------------------------
def test_compute_next_run_interval_and_cron():
    after = dt.datetime(2026, 10, 28, 1, 0, tzinfo=dt.timezone.utc)  # 07:00 in Dhaka
    assert compute_next_run("interval", 90, after=after) == after + timedelta(minutes=90)
    assert compute_next_run("cron", cron_expression="0 */3 * * *", after=after) == dt.datetime(2026, 10, 28, 9, 0, tzinfo=DHAKA)
    assert compute_next_run("manual", after=after) is None
    assert validate_cron("0 */3 * * *") == "" and "5 fields" in validate_cron("every hour")


def test_active_hours_including_overnight():
    from datetime import time

    morning = dt.datetime(2026, 10, 28, 3, 0, tzinfo=dt.timezone.utc)  # 09:00 Dhaka
    night = dt.datetime(2026, 10, 28, 17, 0, tzinfo=dt.timezone.utc)  # 23:00 Dhaka
    assert within_active_hours(time(6), time(18), morning) and not within_active_hours(time(6), time(18), night)
    assert within_active_hours(time(22), time(6), night) and not within_active_hours(time(22), time(6), morning)
    assert within_active_hours(None, None, night)


def test_feed_save_keeps_next_run_in_step(feed):
    assert feed.next_run_at is None  # inactive
    feed.is_active, feed.schedule_type, feed.interval_minutes = True, "interval", 60
    feed.save()
    assert feed.next_run_at is not None and feed.next_run_at <= timezone.now()  # activated -> run soon
    feed.next_run_at = timezone.now() + timedelta(minutes=30)
    feed.save()
    assert feed.next_run_at > timezone.now()  # unrelated save keeps the schedule
    feed.interval_minutes = 240
    feed.save()
    assert feed.next_run_at > timezone.now() + timedelta(minutes=200)  # schedule change -> recomputed
    feed.is_active = False
    feed.save()
    assert feed.next_run_at is None


# --- worker --------------------------------------------------------------------------
def test_worker_runs_due_feeds_and_advances_schedule(scheduled, static_docs):
    static_docs.append(_doc("https://x/1", "Scheduled bulletin text"))
    worker = Worker(inline=True)
    assert worker.dispatch() == [scheduled.pk]
    scheduled.refresh_from_db()
    run = FetchRun.objects.get()
    assert run.trigger == "schedule" and run.status == "success" and CollectedMessage.objects.count() == 1
    assert scheduled.next_run_at > timezone.now() + timedelta(minutes=55)
    assert worker.dispatch() == []  # not due again yet


def test_worker_skips_inactive_manual_and_outside_hours(feed, static_docs):
    from datetime import time

    worker = Worker(inline=True)
    feed.is_active, feed.schedule_type = True, "manual"
    feed.save()
    assert worker.dispatch() == []
    feed.schedule_type = "interval"
    feed.active_hours_start, feed.active_hours_end = time(6), time(18)
    feed.save()
    night = dt.datetime(2026, 10, 28, 17, 0, tzinfo=dt.timezone.utc)
    Feed.objects.filter(pk=feed.pk).update(next_run_at=night - timedelta(minutes=1))
    assert worker.dispatch(now=night) == []
    feed.refresh_from_db()
    assert feed.next_run_at > night  # pushed forward, not run


def test_run_in_background_request_is_picked_up(feed, static_docs, operator_client):
    static_docs.append(_doc("https://x/q", "Queued bulletin text"))
    response = operator_client.post(reverse("ui:feed_queue_run", args=[feed.pk]), follow=True)
    assert "no worker is running" in response.content.decode()
    assert Worker(inline=True).dispatch() == [feed.pk]  # inactive feed, but explicitly requested
    run = FetchRun.objects.get()
    assert run.trigger == "manual"
    feed.refresh_from_db()
    assert feed.run_requested_at is None


def test_run_worker_once_command_and_heartbeat(scheduled, static_docs, capsys):
    static_docs.append(_doc("https://x/o", "Once bulletin text"))
    call_command("run_worker", "--once")
    assert "ran 1 feed" in capsys.readouterr().out
    assert CollectedMessage.objects.count() == 1
    assert not WorkerHeartbeat.objects.exists()  # removed on shutdown
    worker = Worker(inline=True, name="test-worker")
    worker.heartbeat()
    assert WorkerHeartbeat.objects.get(name="test-worker").alive


# --- health --------------------------------------------------------------------------
def _run(feed, status="success", minutes_ago=0, **stats):
    run = FetchRun.objects.create(feed=feed, trigger="schedule", status=status, stats=stats,
                                  error_message="boom" if status == "failed" else "")
    FetchRun.objects.filter(pk=run.pk).update(started_at=timezone.now() - timedelta(minutes=minutes_ago))
    return run


def test_health_states(scheduled):
    assert evaluate(scheduled) == ("unknown", "Not run yet.")
    _run(scheduled, items=3, body_chars=3000, minutes_ago=30)
    Feed.objects.filter(pk=scheduled.pk).update(last_success_at=timezone.now(), last_new_message_at=timezone.now())
    scheduled.refresh_from_db()
    assert evaluate(scheduled)[0] == "healthy"

    Feed.objects.filter(pk=scheduled.pk).update(expected_frequency_hours=6,
                                                last_new_message_at=timezone.now() - timedelta(hours=10))
    scheduled.refresh_from_db()
    status, detail = evaluate(scheduled)
    assert status == "silent" and "expected at least every 6 h" in detail

    Feed.objects.filter(pk=scheduled.pk).update(consecutive_failures=3, last_error="HTTP 503 for https://x")
    scheduled.refresh_from_db()
    status, detail = evaluate(scheduled)
    assert status == "failing" and "HTTP 503" in detail

    scheduled.is_active = False
    assert evaluate(scheduled)[0] == "disabled"


def test_layout_change_heuristic(scheduled):
    history = [_run(scheduled, items=4, body_chars=4000, minutes_ago=m) for m in (300, 240, 180)]
    empty = _run(scheduled, items=0, minutes_ago=60)
    assert "extracted nothing" in layout_change(empty, history)
    tiny = _run(scheduled, items=4, body_chars=80, minutes_ago=30)
    assert "average text is 20 characters" in layout_change(tiny, history)
    normal = _run(scheduled, items=4, body_chars=3600, minutes_ago=10)
    assert layout_change(normal, history) == ""
    # listing feeds measure links found, so 'nothing new' runs are not layout changes
    listing_history = [_run(scheduled, links_found=20, items=2, minutes_ago=m) for m in (500, 400)]
    quiet = _run(scheduled, links_found=20, items=0, minutes_ago=5)
    assert layout_change(quiet, listing_history) == ""

    Feed.objects.filter(pk=scheduled.pk).update(last_success_at=timezone.now(), last_new_message_at=timezone.now())
    scheduled.refresh_from_db()
    _run(scheduled, items=0, minutes_ago=0)
    status, detail = evaluate(scheduled)
    assert status == "degraded" and "layout change" in detail


def test_real_runs_update_health_and_dashboard(scheduled, static_docs, operator_client):
    from apps.collection.pipeline import run_feed

    static_docs.append(_doc("https://x/h", "Health bulletin text"))
    run_feed(scheduled)
    scheduled.refresh_from_db()
    assert scheduled.health_status == "healthy"
    scheduled.method = "missing_method"
    scheduled.save()
    for _ in range(3):
        run_feed(scheduled)
    scheduled.refresh_from_db()
    assert scheduled.health_status == "failing"
    assert refresh_all_health()["failing"] == 1
    page = operator_client.get(reverse("ui:dashboard")).content.decode()
    assert "needs attention" in page and "No background worker is running" in page and "missing_method" in page
