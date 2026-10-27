"""Group events into applications.

Primary key is application_key from the classifier. Near-misses get a
cheap fuzzy merge (same company token + similar role) so "Delivery Hero"
and "delivery hero" don't become two rows. Newsletters stay out.

applied_at only comes from application_sent/ack — rejection-only rows
leave it null so days_to_* stay honest.
"""

from __future__ import annotations

import logging
import uuid
from collections import defaultdict
from datetime import datetime
from difflib import SequenceMatcher

from jobmail import SCHEMA_VERSION
from jobmail.models import Application, Event
from jobmail.normalize import (
    normalize_company,
    normalize_role,
    normalize_skills,
)

logger = logging.getLogger(__name__)

NOISE_TYPES = frozenset({"newsletter_noise"})
APPLIED_TYPES = frozenset({"application_sent", "application_ack"})
RESPONSE_TYPES = frozenset(
    {
        "update",
        "interview_invite",
        "interview_followup",
        "rejection",
        "offer",
        "assessment",
    }
)
INTERVIEW_TYPES = frozenset({"interview_invite", "interview_followup"})
DECISION_TYPES = frozenset({"rejection", "offer"})

_STATUS_RANK = {
    "offer": 60,
    "rejection": 50,
    "interview_followup": 40,
    "interview_invite": 35,
    "assessment": 30,
    "update": 20,
    "application_ack": 15,
    "application_sent": 10,
    "other": 5,
}


def cluster_events(events: list[Event]) -> list[Application]:
    usable = [e for e in events if e.event_type not in NOISE_TYPES]
    if not usable:
        return []

    groups: dict[str, list[Event]] = defaultdict(list)
    for event in usable:
        key = (event.application_key or "").strip().lower()
        if not key:
            key = f"unknown|{event.message_id}"
        groups[key].append(event)

    merged = _fuzzy_merge_groups(dict(groups))
    apps = [_build_application(key, group) for key, group in sorted(merged.items())]
    logger.info("clustered %s events into %s applications", len(usable), len(apps))
    return apps


def _fuzzy_merge_groups(groups: dict[str, list[Event]]) -> dict[str, list[Event]]:
    keys = list(groups.keys())
    parent = {k: k for k in keys}

    def find(k: str) -> str:
        while parent[k] != k:
            parent[k] = parent[parent[k]]
            k = parent[k]
        return k

    for i, a in enumerate(keys):
        for b in keys[i + 1 :]:
            if _keys_compatible(a, b):
                ra, rb = find(a), find(b)
                if ra != rb:
                    parent[rb] = ra

    merged: dict[str, list[Event]] = defaultdict(list)
    for key, evs in groups.items():
        merged[find(key)].extend(evs)
    return dict(merged)


def _keys_compatible(a: str, b: str) -> bool:
    ca, ra = _split_key(a)
    cb, rb = _split_key(b)
    if not ca or not cb or not ra or not rb:
        return False
    company_ok = ca == cb or ca in cb or cb in ca
    if not company_ok:
        return False
    ratio = SequenceMatcher(None, ra, rb).ratio()
    return ratio >= 0.72


def _split_key(key: str) -> tuple[str, str]:
    if "|" not in key:
        return normalize_company(key), ""
    company, _, role = key.partition("|")
    return normalize_company(company), normalize_role(role)


def _build_application(key: str, events: list[Event]) -> Application:
    events = sorted(events, key=lambda e: e.date or "")
    company = _most_common(e.company for e in events if e.company) or key.split("|")[0]
    role = _most_common(e.role_title for e in events if e.role_title) or (
        key.split("|")[1] if "|" in key else ""
    )
    source = _most_common(e.source for e in events if e.source) or "other"

    # only real apply/ack counts — no fallback to rejection date
    applied_at = _first_date(events, APPLIED_TYPES)
    first_response_at = _first_date(events, RESPONSE_TYPES)
    interview_at = _first_date(events, INTERVIEW_TYPES)
    decision_at = _first_date(events, DECISION_TYPES)
    last_event_at = events[-1].date if events else None

    status = _derive_status(events)
    job_url = next((e.job_url for e in events if e.job_url), None)

    skills: list[str] = []
    seen_skills: set[str] = set()
    salary_text = None
    for event in events:
        for skill in event.skills:
            low = skill.lower()
            if low not in seen_skills:
                seen_skills.add(low)
                skills.append(skill)
        if event.salary_text and not salary_text:
            salary_text = event.salary_text

    company_norm = normalize_company(company)
    role_norm = normalize_role(role)
    skills_norm = normalize_skills(skills)

    return Application(
        schema_version=SCHEMA_VERSION,
        application_id=_stable_id(key),
        application_key=key,
        company=company,
        role_title=role,
        source=source,
        status=status,
        applied_at=applied_at,
        last_event_at=last_event_at,
        first_response_at=first_response_at,
        interview_at=interview_at,
        decision_at=decision_at,
        days_to_first_response=_day_delta(applied_at, first_response_at),
        days_to_interview=_day_delta(applied_at, interview_at),
        days_to_decision=_day_delta(applied_at, decision_at),
        event_count=len(events),
        message_ids=[e.message_id for e in events],
        job_url=job_url,
        enrich_status="no_url" if not job_url else "retry",
        salary_text=salary_text,
        skills=skills,
        company_norm=company_norm,
        role_title_norm=role_norm,
        skills_norm=skills_norm,
    )


def _derive_status(events: list[Event]) -> str:
    best = "other"
    best_rank = -1
    for event in events:
        rank = _STATUS_RANK.get(event.event_type, 0)
        if rank >= best_rank:
            best_rank = rank
            best = event.event_type
    if best in {"application_sent", "application_ack"}:
        return "applied"
    if best == "rejection":
        return "rejected"
    if best == "offer":
        return "offer"
    if best in INTERVIEW_TYPES:
        return "interviewing"
    if best == "assessment":
        return "assessment"
    if best == "update":
        return "in_progress"
    return best


def _first_date(events: list[Event], types: frozenset[str]) -> str | None:
    for event in events:
        if event.event_type in types and event.date:
            return event.date
    return None


def _day_delta(start: str | None, end: str | None) -> float | None:
    if not start or not end:
        return None
    try:
        a = datetime.fromisoformat(start)
        b = datetime.fromisoformat(end)
        return round((b - a).total_seconds() / 86400.0, 2)
    except Exception:
        return None


def _most_common(values) -> str | None:
    counts: dict[str, int] = {}
    for value in values:
        if not value:
            continue
        counts[value] = counts.get(value, 0) + 1
    if not counts:
        return None
    return max(counts.items(), key=lambda item: item[1])[0]


def _stable_id(key: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"jobmail:{key}"))
