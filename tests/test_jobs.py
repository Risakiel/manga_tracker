import os

os.environ["ENABLE_SCHEDULER"] = "false"

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.database import get_session
from app.main import app
from app.models import ScheduledJob
from app.services import jobs, scheduler

engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
SQLModel.metadata.create_all(engine)


def _override_get_session():
    with Session(engine) as session:
        yield session


@pytest.fixture()
def client():
    app.dependency_overrides[get_session] = _override_get_session
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def fake_job(monkeypatch):
    """Point run_job/scheduler at the in-memory DB and swap in a job that
    doesn't hit the network."""
    from contextlib import contextmanager

    @contextmanager
    def _scope():
        with Session(engine) as session:
            yield session

    monkeypatch.setattr(jobs, "session_scope", _scope)
    monkeypatch.setattr(scheduler, "session_scope", _scope)
    job = jobs.JobDef(
        id="fake_job",
        label="Fake",
        description="",
        run=lambda session, progress: (progress(1, 1), {"matched": 3})[1],
        default_interval_minutes=lambda: 60,
        result_keys=("matched",),
        summary=lambda r: f"{r['matched']} lié(s)",
    )
    jobs._register(job)
    yield job
    jobs.JOBS.pop("fake_job", None)


def test_menu_has_jobs_page_and_no_excel_import(client):
    resp = client.get("/config")
    assert resp.status_code == 200
    assert 'href="/jobs"' in resp.text
    assert "Import Excel" not in resp.text
    # The import page itself is kept, just no longer linked.
    assert client.get("/import").status_code == 200


def test_jobs_page_lists_every_job(client):
    resp = client.get("/jobs")
    assert resp.status_code == 200
    for job_id in ("suwayomi_sync", "komga_sync", "komga_new_series", "library_sync", "manga_sources_sync"):
        assert f"/jobs/{job_id}/run" in resp.text
    assert "Toutes les semaines" in resp.text


def test_set_interval_persists_override(client):
    resp = client.post("/jobs/komga_sync/interval", data={"minutes": "120"}, follow_redirects=False)
    assert resp.status_code == 303
    with Session(engine) as session:
        assert session.get(ScheduledJob, "komga_sync").interval_minutes == 120
        assert jobs.interval_minutes("komga_sync", session) == 120


def test_run_job_records_last_run(fake_job):
    assert jobs.run_job("fake_job") is True
    assert fake_job.state["running"] is False
    assert fake_job.state["matched"] == 3
    with Session(engine) as session:
        row = session.get(ScheduledJob, "fake_job")
        assert row.last_status == "success"
        assert row.last_finished_at is not None
        assert "3 lié(s)" in row.last_message


def test_run_job_records_errors(fake_job):
    def _boom(session, progress):
        raise RuntimeError("boom")

    fake_job.run = _boom
    jobs.run_job("fake_job")
    assert fake_job.state["error"] == "boom"
    with Session(engine) as session:
        assert session.get(ScheduledJob, "fake_job").last_status == "error"


def test_run_job_skips_when_already_running(fake_job):
    fake_job.lock.acquire()
    try:
        assert jobs.run_job("fake_job") is False
        assert jobs.start_in_background("fake_job") is False
    finally:
        fake_job.lock.release()


def test_scheduled_jobs_are_not_paused(fake_job):
    from apscheduler.schedulers.background import BackgroundScheduler

    sched = BackgroundScheduler(timezone="UTC")
    scheduler._schedule(sched, "fake_job")
    sched.start(paused=True)
    try:
        job = sched.get_job("fake_job")
        assert job is not None
        assert job.next_run_time is not None
    finally:
        sched.shutdown(wait=False)


def test_schedule_resumes_from_last_run_and_honours_disable(fake_job):
    from apscheduler.schedulers.background import BackgroundScheduler

    last = datetime.now(timezone.utc) - timedelta(minutes=10)
    with Session(engine) as session:
        session.merge(ScheduledJob(job_id="fake_job", last_finished_at=last))
        session.commit()
    sched = BackgroundScheduler(timezone="UTC")
    sched.start(paused=True)
    try:
        scheduler._schedule(sched, "fake_job")
        next_run = sched.get_job("fake_job").next_run_time
        assert abs((next_run - (last + timedelta(minutes=60))).total_seconds()) < 5

        with Session(engine) as session:
            jobs.set_interval(session, "fake_job", 0)
        scheduler._schedule(sched, "fake_job")
        assert sched.get_job("fake_job") is None
    finally:
        sched.shutdown(wait=False)


def test_run_job_writes_history_entry(fake_job):
    from sqlmodel import select

    from app.models import SyncLog, SyncSource, SyncStatus

    jobs.run_job("fake_job", "planifié")
    with Session(engine) as session:
        log = session.exec(
            select(SyncLog).where(SyncLog.job_id == "fake_job").order_by(SyncLog.id.desc())
        ).first()
    assert log.source == SyncSource.job
    assert log.status == SyncStatus.success
    assert log.trigger == "planifié"
    assert log.finished_at is not None
    assert "3 lié(s)" in log.message


def test_mark_interrupted_runs(fake_job):
    from app.models import SyncLog, SyncSource, SyncStatus

    with Session(engine) as session:
        log = SyncLog(source=SyncSource.job, status=SyncStatus.running, job_id="fake_job", trigger="manuel")
        session.add(log)
        session.commit()
        log_id = log.id
    jobs.mark_interrupted_runs()
    with Session(engine) as session:
        log = session.get(SyncLog, log_id)
        assert log.status == SyncStatus.error
        assert "interrompue" in log.message


def test_logs_filters(client):
    from app.models import Manga, SyncLog, SyncSource, SyncStatus

    with Session(engine) as session:
        manga = Manga(title_en="Filtre Manga", server_folder="Filtre Manga")
        session.add(manga)
        session.commit()
        session.add_all(
            [
                SyncLog(
                    source=SyncSource.job,
                    status=SyncStatus.success,
                    job_id="komga_sync",
                    trigger="planifié",
                    message="job-marker-planned",
                    created_at=datetime(2026, 10, 9, 12, 0),
                    finished_at=datetime(2026, 10, 9, 12, 2, 5),
                ),
                SyncLog(
                    source=SyncSource.mangaupdates,
                    status=SyncStatus.error,
                    manga_id=manga.id,
                    message="mu-marker-error",
                    created_at=datetime(2026, 10, 1, 12, 0),
                ),
            ]
        )
        session.commit()

    resp = client.get("/logs")
    assert "job-marker-planned" in resp.text and "mu-marker-error" in resp.text
    assert "2 min 05 s" in resp.text
    assert "Filtre Manga" in resp.text

    resp = client.get("/logs", params={"source": "job", "trigger": "planifié"})
    assert "job-marker-planned" in resp.text and "mu-marker-error" not in resp.text

    resp = client.get("/logs", params={"status": "error"})
    assert "job-marker-planned" not in resp.text and "mu-marker-error" in resp.text

    resp = client.get("/logs", params={"date_from": "2026-10-05"})
    assert "job-marker-planned" in resp.text and "mu-marker-error" not in resp.text

    resp = client.get("/logs", params={"q": "filtre manga"})
    assert "job-marker-planned" not in resp.text and "mu-marker-error" in resp.text
