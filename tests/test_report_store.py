"""Report + store roundtrip without Gemini."""

from __future__ import annotations

from pathlib import Path

from jobmail import SCHEMA_VERSION
from jobmail.models import Event
from jobmail.report import write_reports
from jobmail.store import load_events, processed_ids


def test_events_roundtrip(tmp_path: Path):
    events = [
        Event(
            schema_version=SCHEMA_VERSION,
            message_id="<a@x>",
            date="2026-01-05T10:00:00+01:00",
            event_type="application_ack",
            company="Acme",
            role_title="Engineer",
            application_key="acme|engineer",
            source="direct",
            job_url=None,
            confidence=0.8,
            summary="ack",
            source_plugin="apple",
            skills=["Python"],
        )
    ]
    paths = write_reports(tmp_path, events, applications=[])
    assert paths["events_csv"].exists()
    assert paths["events_jsonl"].exists()
    assert paths["applications_csv"].exists()
    assert paths["stats_json"].exists()

    loaded = load_events(tmp_path)
    assert processed_ids(loaded) == {"<a@x>"}
    assert loaded[0].skills == ["Python"]
    assert loaded[0].company_norm == "acme"
    assert loaded[0].skills_norm == ["python"]
