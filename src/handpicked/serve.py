"""Run the job on the configured schedule until stopped."""

import logging
import signal
from collections.abc import Callable
from datetime import datetime

from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_EXECUTED, EVENT_JOB_MISSED
from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

log = logging.getLogger("handpicked")

JOB_ID = "run"
# A fire missed by less than this (the host asleep, say) still runs.
MISFIRE_GRACE = 60 * 60


def scheduler(schedule: str, job: Callable[[], object]) -> BlockingScheduler:
    scheduler = BlockingScheduler(
        executors={"default": ThreadPoolExecutor(1)},
        job_defaults={
            "coalesce": True,
            "max_instances": 1,
            "misfire_grace_time": MISFIRE_GRACE,
        },
    )
    trigger = CronTrigger.from_crontab(schedule)
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
    """Run `job` on `schedule`. SIGTERM lets a run in progress finish."""
    running = scheduler(schedule, job)

    def stop(signum: int, frame: object) -> None:
        if not running.running:  # a second signal while stopping
            return
        log.info("Stopping (%s)", signal.Signals(signum).name)
        # Waits for a run in progress: stopping between creating a Seerr
        # request and recording it would leave the request untracked.
        running.shutdown(wait=True)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    running.start()
    log.info("Stopped")
    return 0


def _when(moment: datetime | None) -> str:
    return moment.strftime("%a %Y-%m-%d %H:%M %Z (UTC%z)") if moment else "never"
