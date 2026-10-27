"""Cluster logic without touching Gemini."""

from __future__ import annotations

from jobmail import SCHEMA_VERSION
from jobmail.cluster import cluster_events
from jobmail.models import Event


def _event(**kwargs) -> Event:
    base = dict(
        schema_version=SCHEMA_VERSION,
        message_id="<x>",
        date="2026-01-05T10:00:00+01:00",
        event_type="application_sent",
        company="Acme Labs",
        role_title="Platform Engineer",
        application_key="acme labs|platform engineer",
        source="direct",
        job_url="https://jobs.acme.example.com/x",
        confidence=0.9,
        summary="applied",
        source_plugin="apple",
        company_norm="acme labs",
        role_title_norm="platform engineer",
    )
    base.update(kwargs)
    return Event(**base)


def test_noise_excluded_from_applications():
    events = [
        _event(message_id="<1>", event_type="application_sent"),
        _event(
            message_id="<2>",
            event_type="newsletter_noise",
            application_key="spam|digest",
            company="Jobboard",
            role_title="",
        ),
    ]
    apps = cluster_events(events)
    assert len(apps) == 1
    assert apps[0].company == "Acme Labs"


def test_rejection_latency_needs_apply():
    events = [
        _event(
            message_id="<a>",
            date="2026-01-06T09:00:00+00:00",
            event_type="application_sent",
            company="Beta GmbH",
            role_title="Backend Developer",
            application_key="beta|backend developer",
        ),
        _event(
            message_id="<b>",
            date="2026-01-23T14:30:00+01:00",
            event_type="rejection",
            company="Beta GmbH",
            role_title="Backend Developer",
            application_key="beta|backend developer",
        ),
    ]
    apps = cluster_events(events)
    assert len(apps) == 1
    app = apps[0]
    assert app.status == "rejected"
    assert app.days_to_decision is not None
    assert app.days_to_decision > 15


def test_rejection_only_has_no_latency():
    events = [
        _event(
            message_id="<r>",
            date="2026-01-23T14:30:00+01:00",
            event_type="rejection",
            company="eBay",
            role_title="Director",
            application_key="ebay|director",
        )
    ]
    apps = cluster_events(events)
    assert apps[0].applied_at is None
    assert apps[0].days_to_decision is None


def test_fuzzy_merge_similar_keys():
    events = [
        _event(
            message_id="<1>",
            application_key="delivery hero|software engineer fullstack",
            company="Delivery Hero",
            role_title="Software Engineer Fullstack",
        ),
        _event(
            message_id="<2>",
            date="2026-01-10T12:00:00+00:00",
            event_type="rejection",
            application_key="delivery hero|software engineer i fullstack",
            company="Delivery Hero",
            role_title="Software Engineer I Fullstack",
        ),
    ]
    apps = cluster_events(events)
    assert len(apps) == 1
    assert apps[0].event_count == 2
