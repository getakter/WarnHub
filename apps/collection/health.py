"""Feed health: disabled / unknown / failing / silent / degraded / healthy.

failing   - FAILING_AFTER consecutive failed runs (or never succeeded and the last run failed)
silent    - no new message for longer than the feed's expected frequency
degraded  - last run failed or had errors, most documents failed to extract, or the layout-change
            heuristic fires: a sudden empty extraction, or text size very different from recent runs
"""
from statistics import median

from django.utils import timezone
from django.utils.timesince import timesince

from apps.sources.models import Feed

FAILING_AFTER = 3
HISTORY = 5
SIZE_LOW, SIZE_HIGH = 0.2, 5.0
MIN_CHARS_FOR_SIZE_CHECK = 200


def _signal(stats):
    """How much a run extracted: links found on listings, otherwise items processed."""
    return stats.get("links_found", stats.get("items", 0))


def _avg_chars(stats):
    items = stats.get("items", 0)
    return stats.get("body_chars", 0) / items if items else None


def layout_change(latest, previous):
    """Return a human-readable reason when the latest run looks unlike the recent ones, else ''."""
    history = [r.stats or {} for r in previous if r.status in {"success", "partial"}]
    stats = latest.stats or {}
    if len(history) >= 2:
        usual = median(_signal(s) for s in history)
        if usual > 0 and _signal(stats) == 0:
            return (f"Possible layout change: this run extracted nothing, recent runs extracted about {usual:g} "
                    "item(s)/link(s). Check the selectors with Inspect URL.")
        sizes = [a for a in (_avg_chars(s) for s in history) if a]
        current = _avg_chars(stats)
        if len(sizes) >= 2 and current is not None:
            usual_size = median(sizes)
            if usual_size >= MIN_CHARS_FOR_SIZE_CHECK and not (SIZE_LOW * usual_size <= current <= SIZE_HIGH * usual_size):
                return (f"Possible layout change: average text is {current:.0f} characters per item, usually about "
                        f"{usual_size:.0f}. Check the extraction rules.")
    documents = stats.get("documents", 0)
    failed = stats.get("extraction_failed", 0)
    if documents and failed >= max(1, documents / 2):
        return f"{failed} of {documents} documents could not be extracted."
    return ""


def evaluate(feed, now=None):
    now = now or timezone.now()
    if not feed.is_active:
        return "disabled", "Feed is disabled."
    runs = list(
        feed.runs.filter(dry_run=False, trigger__in=["schedule", "manual"]).exclude(status__in=["skipped", "running"])
        .order_by("-started_at")[: HISTORY + 1]
    )
    if not runs:
        return "unknown", "Not run yet."
    latest = runs[0]
    if feed.consecutive_failures >= FAILING_AFTER or (latest.status == "failed" and not feed.last_success_at):
        error = (feed.last_error or latest.error_message or "unknown error").splitlines()[0][:300]
        return "failing", f"{max(feed.consecutive_failures, 1)} failed run(s) in a row. Last error: {error}"
    if feed.expected_frequency_hours:
        reference = feed.last_new_message_at or feed.last_success_at or feed.created_at
        if (now - reference).total_seconds() > feed.expected_frequency_hours * 3600:
            return "silent", (f"No new message for {timesince(reference, now)} "
                              f"(expected at least every {feed.expected_frequency_hours:g} h).")
    problems = []
    if latest.status == "failed":
        problems.append("Last run failed: " + (latest.error_message or "").splitlines()[0][:300] if latest.error_message else "Last run failed.")
    elif latest.status == "partial":
        problems.append("Last run had errors: " + (latest.error_message or "").splitlines()[0][:300])
    change = layout_change(latest, runs[1:])
    if change:
        problems.append(change)
    if problems:
        return "degraded", " ".join(problems)
    last_ok = feed.last_success_at or latest.started_at
    return "healthy", f"Last successful run {timesince(last_ok, now)} ago."


def refresh_feed_health(feed, now=None):
    status, detail = evaluate(feed, now)
    Feed.objects.filter(pk=feed.pk).update(health_status=status, health_detail=detail[:2000])
    feed.health_status, feed.health_detail = status, detail
    return status


def refresh_all_health(now=None):
    counts = {}
    for feed in Feed.objects.all():
        status = refresh_feed_health(feed, now)
        counts[status] = counts.get(status, 0) + 1
    return counts
