import logging
import signal
import threading
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
import pytest
import respx
import tzlocal.unix

from handpicked import serve


@pytest.fixture(autouse=True)
def stray_sigterm_fails_the_test():
    """A SIGTERM that serve did not handle fails the test instead of pytest."""

    def stray(signum, frame):
        raise RuntimeError("SIGTERM reached the main thread unblocked")

    previous = signal.signal(signal.SIGTERM, stray)
    yield
    signal.signal(signal.SIGTERM, previous)


def stop_main_thread(after=0.2):
    """SIGTERM for the main thread, where serve waits for it."""
    main = threading.main_thread().ident
    timer = threading.Timer(after, signal.pthread_kill, (main, signal.SIGTERM))
    timer.start()
    return timer


def test_scheduler_runs_one_job_at_a_time_and_logs_the_next_run(caplog):
    caplog.set_level(logging.INFO, "handpicked")

    scheduler = serve.scheduler(
        "0 9 * * SUN", lambda: None, ZoneInfo("America/Toronto")
    )

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


def test_sigterm_stops_an_idle_scheduler(caplog):
    caplog.set_level(logging.INFO, "handpicked")
    handler = signal.getsignal(signal.SIGTERM)
    stop_main_thread()
    started = datetime.now()

    assert serve.serve("0 9 * * SUN", lambda: None) == 0
    assert (datetime.now() - started).total_seconds() < 5
    messages = [r.getMessage() for r in caplog.records]
    assert "Stopping (SIGTERM)" in messages
    assert messages[-1] == "Stopped"
    assert signal.getsignal(signal.SIGTERM) is handler


def test_sigterm_before_the_scheduler_starts_is_not_lost(monkeypatch):
    real_scheduler = serve.scheduler

    def stopped_while_building(schedule, job, timezone):
        signal.raise_signal(signal.SIGTERM)
        return real_scheduler(schedule, job, timezone)

    monkeypatch.setattr(serve, "scheduler", stopped_while_building)

    assert serve.serve("0 9 * * SUN", lambda: None) == 0


@pytest.mark.parametrize("tz", ["Toronto", "../etc"])
def test_invalid_tz_is_a_clear_error(monkeypatch, capsys, tz):
    monkeypatch.setenv("TZ", tz)
    monkeypatch.setattr(tzlocal.unix, "_cache_tz", None)  # read once per process
    backstop = stop_main_thread(after=5)  # so a regression fails, not hangs

    try:
        assert serve.serve("0 9 * * SUN", lambda: None) == 2
    finally:
        backstop.cancel()
    assert capsys.readouterr().err.startswith("Cannot determine the time zone")


def test_sigterm_waits_for_a_run_in_progress_and_takes_a_second(monkeypatch):
    stopping = threading.Event()
    finished = threading.Event()

    def job():
        main = threading.main_thread().ident
        signal.pthread_kill(main, signal.SIGTERM)
        stopping.wait(5)
        signal.pthread_kill(main, signal.SIGTERM)  # while shutdown waits for us
        finished.set()

    real_scheduler = serve.scheduler

    def scheduler_running_now(schedule, job, timezone):
        scheduler = real_scheduler(schedule, job, timezone)
        scheduler.get_job(serve.JOB_ID).modify(next_run_time=datetime.now(timezone))
        real_shutdown = scheduler.shutdown

        def shutdown(wait=True):
            stopping.set()
            real_shutdown(wait)

        scheduler.shutdown = shutdown
        return scheduler

    monkeypatch.setattr(serve, "scheduler", scheduler_running_now)

    assert serve.serve("0 9 * * SUN", job) == 0
    assert finished.is_set()


@respx.mock
def test_logging_keeps_request_urls_with_keys_out(caplog):
    httpx_logger, root = logging.getLogger("httpx"), logging.getLogger()
    handlers = list(root.handlers)
    respx.get("http://tautulli:8181/api/v2").respond(json={})
    try:
        serve.configure_logging()
        caplog.set_level(logging.INFO)

        httpx.get("http://tautulli:8181/api/v2?apikey=secret")
    finally:
        httpx_logger.setLevel(logging.NOTSET)
        root.handlers[:] = handlers

    assert "secret" not in caplog.text
