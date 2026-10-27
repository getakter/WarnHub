"""Schedule helpers: cron validation, next run time, active hours. Times are evaluated in Asia/Dhaka."""
from datetime import timedelta

from apscheduler.triggers.cron import CronTrigger
from django.conf import settings
from django.utils import timezone
from zoneinfo import ZoneInfo


def local_tz():
    return ZoneInfo(settings.TIME_ZONE)


def validate_cron(expression):
    """Return an error message, or '' when the 5-field crontab expression is valid."""
    if not expression or len(expression.split()) != 5:
        return "use 5 fields: minute hour day-of-month month day-of-week (e.g. '0 */3 * * *')"
    try:
        CronTrigger.from_crontab(expression, timezone=local_tz())
    except ValueError as exc:
        return str(exc)
    return ""


def within_active_hours(start, end, now=None):
    if not start or not end:
        return True
    local = (now or timezone.now()).astimezone(local_tz()).time()
    if start <= end:
        return start <= local <= end
    return local >= start or local <= end  # window crosses midnight


def compute_next_run(schedule_type, interval_minutes=60, cron_expression="", after=None):
    after = after or timezone.now()
    if schedule_type == "interval":
        return after + timedelta(minutes=max(1, interval_minutes or 1))
    if schedule_type == "cron" and not validate_cron(cron_expression):
        trigger = CronTrigger.from_crontab(cron_expression, timezone=local_tz())
        return trigger.get_next_fire_time(None, after.astimezone(local_tz()))
    return None


def next_run_for_feed(feed, after=None):
    return compute_next_run(feed.schedule_type, feed.interval_minutes, feed.cron_expression, after)
