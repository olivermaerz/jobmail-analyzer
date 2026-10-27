"""Write the wrangling artifacts.

Always emit events.jsonl (via store), events.csv, applications.csv,
applications.json, stats.json. CSVs are the sheet-friendly view.
"""

from __future__ import annotations

import csv
import json
import logging
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from jobmail.models import Application, Event
from jobmail.store import save_events

logger = logging.getLogger(__name__)

# "ghosted" = applied, no real response, and apply is older than this
GHOSTED_AFTER_DAYS = 14

EVENT_CSV_FIELDS = [
    "schema_version",
    "message_id",
    "date",
    "event_type",
    "company",
    "role_title",
    "company_norm",
    "role_title_norm",
    "application_key",
    "source",
    "job_url",
    "confidence",
    "summary",
    "source_plugin",
    "salary_text",
    "skills",
    "skills_norm",
    "from_addr",
    "subject",
]

APP_CSV_FIELDS = [
    "application_id",
    "application_key",
    "company",
    "role_title",
    "company_norm",
    "role_title_norm",
    "source",
    "status",
    "applied_at",
    "last_event_at",
    "first_response_at",
    "interview_at",
    "decision_at",
    "days_to_first_response",
    "days_to_interview",
    "days_to_decision",
    "event_count",
    "job_url",
    "enrich_status",
    "salary_min",
    "salary_max",
    "currency",
    "salary_period",
    "salary_text",
    "skills",
    "skills_norm",
    "location",
    "employment_type",
    "seniority",
    "remote_policy",
    "notes",
]


def write_reports(
    out_dir: Path,
    events: list[Event],
    applications: list[Application],
) -> dict[str, Path]:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    paths: dict[str, Path] = {}
    paths["events_jsonl"] = save_events(out_dir, events)
    paths["events_csv"] = _write_events_csv(out_dir, events)
    paths["applications_csv"] = _write_applications_csv(out_dir, applications)
    paths["applications_json"] = _write_applications_json(out_dir, applications)
    paths["stats_json"] = _write_stats(out_dir, events, applications)
    return paths


def _write_events_csv(out_dir: Path, events: list[Event]) -> Path:
    path = out_dir / "events.csv"
    ordered = sorted(events, key=lambda e: (e.date or "", e.message_id))
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=EVENT_CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for event in ordered:
            row = event.to_dict()
            row["skills"] = ";".join(event.skills)
            row["skills_norm"] = ";".join(event.skills_norm)
            writer.writerow({k: row.get(k, "") for k in EVENT_CSV_FIELDS})
    logger.info("wrote events.csv (%s rows)", len(ordered))
    return path


def _write_applications_csv(out_dir: Path, applications: list[Application]) -> Path:
    path = out_dir / "applications.csv"
    ordered = sorted(applications, key=lambda a: (a.applied_at or "", a.company))
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=APP_CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for app in ordered:
            row = app.to_dict()
            row["skills"] = ";".join(app.skills)
            row["skills_norm"] = ";".join(app.skills_norm)
            writer.writerow({k: row.get(k, "") for k in APP_CSV_FIELDS})
    logger.info("wrote applications.csv (%s rows)", len(ordered))
    return path


def _write_applications_json(out_dir: Path, applications: list[Application]) -> Path:
    path = out_dir / "applications.json"
    payload = [app.to_dict() for app in applications]
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    logger.info("wrote applications.json (%s apps)", len(payload))
    return path


def _write_stats(
    out_dir: Path,
    events: list[Event],
    applications: list[Application],
) -> Path:
    path = out_dir / "stats.json"
    status_counts = Counter(a.status for a in applications)
    event_counts = Counter(e.event_type for e in events)
    enrich_counts = Counter(a.enrich_status for a in applications)

    # only apps with a real apply/ack date contribute to latency averages
    decision_days = [
        a.days_to_decision
        for a in applications
        if a.applied_at and a.days_to_decision is not None
    ]
    response_days = [
        a.days_to_first_response
        for a in applications
        if a.applied_at and a.days_to_first_response is not None
    ]

    cutoff = datetime.now().astimezone() - timedelta(days=GHOSTED_AFTER_DAYS)
    ghosted = 0
    for app in applications:
        if app.status != "applied" or app.first_response_at or not app.applied_at:
            continue
        try:
            applied = datetime.fromisoformat(app.applied_at)
            if applied.tzinfo is None:
                applied = applied.astimezone()
            if applied <= cutoff:
                ghosted += 1
        except Exception:
            continue

    stats: dict[str, Any] = {
        "event_count": len(events),
        "application_count": len(applications),
        "events_by_type": dict(event_counts),
        "applications_by_status": dict(status_counts),
        "enrich_by_status": dict(enrich_counts),
        "avg_days_to_first_response": _avg(response_days),
        "avg_days_to_decision": _avg(decision_days),
        "median_days_to_decision": _median(decision_days),
        "ghosted_after_days": GHOSTED_AFTER_DAYS,
        "ghosted_count": ghosted,
    }
    path.write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("wrote stats.json")
    return path


def _avg(values: list[float]) -> float | None:
    if not values:
        return None
    return round(sum(values) / len(values), 2)


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return round(ordered[mid], 2)
    return round((ordered[mid - 1] + ordered[mid]) / 2.0, 2)
