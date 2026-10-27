"""Apple mbox ingest — uses the anonymized fixture only."""

from __future__ import annotations

from pathlib import Path

from jobmail.ingest.apple_mbox import AppleMboxSource

FIXTURE = Path(__file__).parent / "fixtures" / "sample.mbox"


def test_iter_emails_reads_fixture():
    source = AppleMboxSource()
    emails = list(source.iter_emails(FIXTURE))
    assert len(emails) == 4
    assert all(e.message_id for e in emails)
    assert all(e.source_plugin == "apple" for e in emails)


def test_subjects_and_urls_decoded():
    source = AppleMboxSource()
    emails = {e.message_id: e for e in source.iter_emails(FIXTURE)}
    ack = emails["<ack-001@jobs.example.com>"]
    assert "Platform Engineer" in ack.subject
    assert any("jobs.acme.example.com" in u for u in ack.urls)
    assert "Kubernetes" in ack.body_text


def test_missing_mbox_raises():
    source = AppleMboxSource()
    try:
        list(source.iter_emails(Path("/tmp/does-not-exist-jobmail.mbox")))
        raise AssertionError("expected FileNotFoundError")
    except FileNotFoundError:
        pass
