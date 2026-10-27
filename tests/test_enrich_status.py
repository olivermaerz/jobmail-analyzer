"""Enrich cache status machine — no network, no Gemini."""

from __future__ import annotations

import json
from pathlib import Path

from jobmail import SCHEMA_VERSION
from jobmail.browser_enrich import (
    looks_blocked,
    needs_human_page,
    should_skip_browser_host,
)
from jobmail.enrich import enrich_applications, is_probably_tracking_url
from jobmail.models import Application


def _app(**kwargs) -> Application:
    base = dict(
        schema_version=SCHEMA_VERSION,
        application_id="id-1",
        application_key="acme|platform engineer",
        company="Acme Labs",
        role_title="Platform Engineer",
        source="direct",
        status="applied",
        applied_at="2026-01-05T10:00:00+01:00",
        last_event_at="2026-01-05T10:00:00+01:00",
        first_response_at=None,
        interview_at=None,
        decision_at=None,
        days_to_first_response=None,
        days_to_interview=None,
        days_to_decision=None,
        event_count=1,
        message_ids=["<ack-001@jobs.example.com>"],
        job_url="https://jobs.acme.example.com/positions/platform-engineer-42",
        enrich_status="retry",
        skills=[],
        company_norm="acme labs",
        role_title_norm="platform engineer",
    )
    base.update(kwargs)
    return Application(**base)


def test_skip_enrich_marks_no_url(tmp_path: Path):
    app = _app(job_url=None)
    enrich_applications([app], tmp_path, api_key=None, skip=True)
    assert app.enrich_status == "no_url"


def test_terminal_extracted_cache_not_refetched(tmp_path: Path):
    app = _app()
    cache_dir = tmp_path / "job_ads"
    cache_dir.mkdir()
    from jobmail.enrich import _url_hash

    cache_path = cache_dir / f"{_url_hash(app.job_url)}.json"
    cache_path.write_text(
        json.dumps(
            {
                "status": "extracted",
                "url": app.job_url,
                "extracted": {
                    "posting_active": True,
                    "salary_min": 70000,
                    "salary_max": 85000,
                    "currency": "EUR",
                    "salary_period": "year",
                    "salary_text": "70-85k",
                    "skills": ["Kubernetes", "Python"],
                    "location": "Berlin",
                    "employment_type": "full-time",
                    "seniority": "mid",
                    "remote_policy": "hybrid",
                    "notes": "from cache",
                },
            }
        ),
        encoding="utf-8",
    )

    enrich_applications([app], tmp_path, api_key="unused", skip=False)
    assert app.enrich_status == "extracted"
    assert app.salary_min == 70000
    assert "Kubernetes" in app.skills
    assert app.location == "Berlin"


def test_blocked_cache_is_terminal(tmp_path: Path):
    app = _app()
    from jobmail.enrich import _url_hash

    cache_dir = tmp_path / "job_ads"
    cache_dir.mkdir()
    (cache_dir / f"{_url_hash(app.job_url)}.json").write_text(
        json.dumps(
            {
                "status": "blocked",
                "url": app.job_url,
                "error": "bot_or_login_wall",
            }
        ),
        encoding="utf-8",
    )
    enrich_applications([app], tmp_path, api_key="unused", skip_browser=True)
    assert app.enrich_status == "blocked"


def test_needs_human_page_login_wall():
    html = "<html>Please sign in to continue. Log in below.</html>"
    assert needs_human_page(html)


def test_looks_blocked_cloudflare():
    assert looks_blocked("cf-mitigated challenge-platform", 403)
    assert looks_blocked("<html>Just a moment... cloudflare</html>", 200)
    assert not looks_blocked(
        "<html>" + ("job description kubernetes " * 500) + "</html>",
        200,
    )


def test_tracking_url_helper():
    assert is_probably_tracking_url("https://click.stepstone.de/f/a/xyz")
    assert is_probably_tracking_url("https://engage.indeed.com/f/a/abc")
    assert not is_probably_tracking_url("https://jobs.acme.example.com/role")


def test_indeed_skipped_for_browser_by_default():
    assert should_skip_browser_host(
        "de.indeed.com", skip_hosts=("indeed.com",)
    )
    assert not should_skip_browser_host(
        "de.indeed.com",
        force_reauth_hosts={"de.indeed.com"},
        skip_hosts=("indeed.com",),
    )
    assert not should_skip_browser_host(
        "jobs.acme.example.com", skip_hosts=("indeed.com",)
    )
