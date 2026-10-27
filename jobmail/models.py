"""Shared dataclasses for the pipeline.

RawEmail is the only thing ingest plugins emit. Everything after that
works off Event / Application records so Gmail/Outlook don't leak in.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class RawEmail:
    """Normalized mail from any ingest plugin."""

    message_id: str
    date: datetime | None
    from_addr: str
    to_addr: str
    subject: str
    body_text: str
    body_html: str
    urls: list[str]
    source_plugin: str
    headers: dict[str, str] = field(default_factory=dict)


# Event types the classifier is allowed to emit.
EVENT_TYPES = (
    "application_sent",
    "application_ack",
    "update",
    "interview_invite",
    "interview_followup",
    "rejection",
    "offer",
    "assessment",
    "newsletter_noise",
    "other",
)

# Terminal enrich statuses — don't refetch these.
ENRICH_TERMINAL = frozenset({"extracted", "inactive", "no_url", "blocked"})
# Transient — try again on the next run.
ENRICH_RETRY = frozenset({"retry"})


@dataclass
class Event:
    """One classified email. Flat enough to dump straight into CSV."""

    schema_version: int
    message_id: str
    date: str | None
    event_type: str
    company: str
    role_title: str
    application_key: str
    source: str
    job_url: str | None
    confidence: float
    summary: str
    source_plugin: str
    salary_text: str | None = None
    skills: list[str] = field(default_factory=list)
    company_norm: str = ""
    role_title_norm: str = ""
    skills_norm: list[str] = field(default_factory=list)
    from_addr: str = ""
    subject: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Application:
    """One job application = clustered events + optional ad enrich."""

    schema_version: int
    application_id: str
    application_key: str
    company: str
    role_title: str
    source: str
    status: str
    applied_at: str | None
    last_event_at: str | None
    first_response_at: str | None
    interview_at: str | None
    decision_at: str | None
    days_to_first_response: float | None
    days_to_interview: float | None
    days_to_decision: float | None
    event_count: int
    message_ids: list[str]
    job_url: str | None
    enrich_status: str
    salary_min: float | None = None
    salary_max: float | None = None
    currency: str | None = None
    salary_period: str | None = None
    salary_text: str | None = None
    skills: list[str] = field(default_factory=list)
    company_norm: str = ""
    role_title_norm: str = ""
    skills_norm: list[str] = field(default_factory=list)
    location: str | None = None
    employment_type: str | None = None
    seniority: str | None = None
    remote_policy: str | None = None
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
