"""Run the job on the configured schedule until stopped."""

import logging
import signal
import sys
import time
from collections.abc import Callable
from datetime import datetime, tzinfo
from zoneinfo import ZoneInfoNotFoundError

import tzlocal
from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_EXECUTED, EVENT_JOB_MISSED
from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

log = logging.getLogger("handpicked")

JOB_ID = "run"
# A fire missed by less than this (the host asleep, say) still runs.
MISFIRE_GRACE = 60 * 60
STOP_SIGNALS = (signal.SIGTERM, signal.SIGINT)
# How often the main thread checks whether a stop signal arrived.
STOP_POLL = 0.5


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # httpx logs every request URL at INFO, and Tautulli takes its API key
    # as a query parameter.
    logging.getLogger("httpx").setLevel(logging.WARNING)


def scheduler(
    schedule: str, job: Callable[[], object], timezone: tzinfo
) -> BackgroundScheduler:
    scheduler = BackgroundScheduler(
        timezone=timezone,
        executors={"default": ThreadPoolExecutor(1)},
        job_defaults={
            "coalesce": True,
            "max_instances": 1,
            "misfire_grace_time": MISFIRE_GRACE,
        },
    )
    trigger = CronTrigger.from_crontab(schedule, timezone)
    scheduler.add_job(job, trigger, id=JOB_ID, name="handpicked run")

    def next_run(event=None) -> None:
        if event is not None and event.code == EVENT_JOB_MISSED:
            log.warning("Skipped the run due at %s", _when(event.scheduled_run_time))
        now = datetime.now(trigger.timezone)
        log.info("Next run: %s", _when(trigger.get_next_fire_time(None, now)))

    scheduler.add_listener(
        next_run, EVENT_JOB_EXECUTED | EVENT_JOB_ERROR | EVENT_JOB_MISSED
    )
    next_run()
    return scheduler


def serve(schedule: str, job: Callable[[], object]) -> int:
    """Run `job` on `schedule` until SIGTERM, letting a run in progress finish.

    The signal handler only records the signal, and the main thread stops
    the scheduler: stopping it from the handler, which can interrupt the
    scheduler while it holds its locks, could deadlock waiting for the run.
    """
    received: list[int] = []  # not an Event, whose set() takes a lock

    def stop(signum, frame) -> None:
        received.append(signum)

    previous = {signum: signal.signal(signum, stop) for signum in STOP_SIGNALS}
    try:
        try:
            timezone = tzlocal.get_localzone()
        except (ZoneInfoNotFoundError, ValueError) as e:
            reason = e.args[0] if e.args else e
            print(
                "Cannot determine the time zone (set TZ, e.g. America/Toronto):"
                f" {reason}",
                file=sys.stderr,
            )
            return 2
        running = scheduler(schedule, job, timezone)
        running.start()
        while not received:
            time.sleep(STOP_POLL)
        log.info("Stopping (%s)", signal.Signals(received[0]).name)
        # Waits for a run in progress: stopping between creating a Seerr
        # request and recording it would leave the request untracked.
        running.shutdown(wait=True)
        log.info("Stopped")
        return 0
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def _when(moment: datetime | None) -> str:
    return moment.strftime("%a %Y-%m-%d %H:%M %Z (UTC%z)") if moment else "never"
