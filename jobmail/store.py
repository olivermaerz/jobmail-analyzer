"""Load/save events.jsonl — the incremental brain of the pipeline.

Message-ID is the dedupe key. Re-exports of a bigger mbox just append
new ids; we never re-ask Gemini about mail we already classified.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from jobmail.models import Event
from jobmail.normalize import (
    normalize_company,
    normalize_role,
    normalize_skills,
    resolve_application_key,
)

logger = logging.getLogger(__name__)

EVENTS_JSONL = "events.jsonl"


def events_path(out_dir: Path) -> Path:
    return Path(out_dir) / EVENTS_JSONL


def load_events(out_dir: Path) -> list[Event]:
    path = events_path(out_dir)
    if not path.exists():
        return []

    events: list[Event] = []
    with path.open(encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
                events.append(_dict_to_event(data))
            except Exception as exc:
                logger.warning("skipping bad events.jsonl line %s: %s", line_no, exc)
    return events


def processed_ids(events: list[Event]) -> set[str]:
    return {e.message_id for e in events}


def save_events(out_dir: Path, events: list[Event]) -> Path:
    """Rewrite the whole jsonl. Small enough that append-only isn't worth it."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = events_path(out_dir)

    # stable order: by date then message_id so diffs aren't chaos
    ordered = sorted(events, key=lambda e: (e.date or "", e.message_id))
    with path.open("w", encoding="utf-8") as fh:
        for event in ordered:
            fh.write(json.dumps(event.to_dict(), ensure_ascii=False) + "\n")
    logger.info("wrote %s events -> %s", len(ordered), path)
    return path


def _dict_to_event(data: dict[str, Any]) -> Event:
    company = data.get("company") or ""
    role = data.get("role_title") or ""
    skills = list(data.get("skills") or [])
    company_norm = data.get("company_norm") or normalize_company(company)
    role_norm = data.get("role_title_norm") or normalize_role(role)
    skills_norm = list(data.get("skills_norm") or normalize_skills(skills))
    # re-derive unless we already have a key (catalog aliases stay intact)
    app_key = data.get("application_key") or ""
    if not app_key:
        app_key = resolve_application_key(company, role, None)

    return Event(
        schema_version=int(data.get("schema_version", 1)),
        message_id=data["message_id"],
        date=data.get("date"),
        event_type=data.get("event_type", "other"),
        company=company,
        role_title=role,
        application_key=app_key,
        source=data.get("source") or "",
        job_url=data.get("job_url"),
        confidence=float(data.get("confidence") or 0.0),
        summary=data.get("summary") or "",
        source_plugin=data.get("source_plugin") or "",
        salary_text=data.get("salary_text"),
        skills=skills,
        company_norm=company_norm,
        role_title_norm=role_norm,
        skills_norm=skills_norm,
        from_addr=data.get("from_addr") or "",
        subject=data.get("subject") or "",
    )
