"""Norm helpers + classify catalog rendering."""

from __future__ import annotations

from jobmail import SCHEMA_VERSION
from jobmail.catalog import build_catalog_text, collect_catalog_keys
from jobmail.models import Application, Event
from jobmail.normalize import (
    application_key,
    normalize_company,
    normalize_role,
    normalize_skills,
    resolve_application_key,
)


def test_normalize_company_strips_legal_fluff():
    assert normalize_company("Dive Solutions GmbH") == "dive solutions"
    assert normalize_company("PTA AG") == "pta"


def test_normalize_role_unwraps_parens_and_cuts_tails():
    role = "Software Engineer I (Fullstack), Dmarts – Quick Commerce"
    assert normalize_role(role) == "software engineer i fullstack"

    role2 = "Senior Platform Engineer (Kubernetes, Python) (m/f/d)"
    assert normalize_role(role2) == "senior platform engineer kubernetes python"


def test_normalize_role_and_key():
    assert "platform" in normalize_role("Senior Platform Engineer (m/f/d)")
    key = application_key(
        "Delivery Hero",
        "Software Engineer I (Fullstack), Dmarts – Quick Commerce",
    )
    assert key == "delivery hero|software engineer i fullstack"


def test_resolve_reuses_exact_catalog_key():
    known = {"dive|senior platform engineer"}
    assert (
        resolve_application_key(
            "Dive Solutions",
            "Senior Platform Engineer (Kubernetes, Python)",
            "dive|senior platform engineer",
            known,
        )
        == "dive|senior platform engineer"
    )
    # invented LLM key is ignored — we derive
    assert (
        resolve_application_key(
            "Delivery Hero",
            "Software Engineer I (Fullstack), Dmarts – Quick Commerce",
            "delivery hero|software engineer i (fullstack), dmarts – quick commerce",
            known,
        )
        == "delivery hero|software engineer i fullstack"
    )


def test_normalize_skills_lowercases():
    assert normalize_skills(["Kubernetes", "Python", "kubernetes"]) == [
        "kubernetes",
        "python",
    ]


def test_catalog_nests_company_roles():
    apps = [
        Application(
            schema_version=SCHEMA_VERSION,
            application_id="1",
            application_key="dive|senior platform engineer",
            company="Dive",
            role_title="Senior Platform Engineer",
            source="stepstone",
            status="rejected",
            applied_at=None,
            last_event_at=None,
            first_response_at=None,
            interview_at=None,
            decision_at=None,
            days_to_first_response=None,
            days_to_interview=None,
            days_to_decision=None,
            event_count=1,
            message_ids=["<a>"],
            job_url=None,
            enrich_status="no_url",
        )
    ]
    text = build_catalog_text(apps)
    assert "company: Dive" in text
    assert "key=dive|senior platform engineer" in text
    assert collect_catalog_keys(apps) == {"dive|senior platform engineer"}


def test_catalog_includes_events():
    events = [
        Event(
            schema_version=SCHEMA_VERSION,
            message_id="<x>",
            date=None,
            event_type="application_sent",
            company="PTA GmbH",
            role_title="Software Engineer",
            application_key="pta|software engineer",
            source="indeed",
            job_url=None,
            confidence=1.0,
            summary="",
            source_plugin="apple",
        )
    ]
    text = build_catalog_text(events=events)
    assert "PTA GmbH" in text
    assert "pta|software engineer" in text
