import logging
import os
import signal
import threading
from datetime import datetime

import pytest

from handpicked import serve


@pytest.fixture
def restore_signals():
    saved = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT)}
    yield
    for sig, handler in saved.items():
        signal.signal(sig, handler)


def test_scheduler_runs_one_job_at_a_time_and_logs_the_next_run(caplog):
    caplog.set_level(logging.INFO, "handpicked")

    scheduler = serve.scheduler("0 9 * * SUN", lambda: None)

    # Job defaults only reach the job once the scheduler starts.
    assert scheduler._job_defaults == {
        "coalesce": True,
        "max_instances": 1,
        "misfire_grace_time": serve.MISFIRE_GRACE,
    }
    (job,) = scheduler.get_jobs()
    assert str(job.trigger.fields[4]) == "sun"
    (record,) = caplog.records
    assert record.getMessage().startswith("Next run: Sun ")
    assert "(UTC" in record.getMessage()


def test_sigterm_stops_an_idle_scheduler(restore_signals, caplog):
    caplog.set_level(logging.INFO, "handpicked")
    threading.Timer(0.2, os.kill, (os.getpid(), signal.SIGTERM)).start()
    started = datetime.now()

    assert serve.serve("0 9 * * SUN", lambda: None) == 0
    assert (datetime.now() - started).total_seconds() < 5
    messages = [r.getMessage() for r in caplog.records]
    assert "Stopping (SIGTERM)" in messages
    assert messages[-1] == "Stopped"


def test_sigterm_waits_for_a_run_in_progress(restore_signals, monkeypatch):
    finished = threading.Event()
    started = threading.Event()

    def job():
        started.set()
        threading.Event().wait(0.5)
        finished.set()

    real_scheduler = serve.scheduler

    def scheduler_running_now(schedule, job):
        scheduler = real_scheduler(schedule, job)
        scheduler.get_job(serve.JOB_ID).modify(
            next_run_time=datetime.now().astimezone()
        )
        return scheduler

    monkeypatch.setattr(serve, "scheduler", scheduler_running_now)

    def stop_once_running():
        started.wait(5)
        os.kill(os.getpid(), signal.SIGTERM)

    threading.Thread(target=stop_once_running).start()

    assert serve.serve("0 9 * * SUN", job) == 0
    assert finished.is_set()
