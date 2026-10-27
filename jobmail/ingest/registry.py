"""Map --source names to plugin instances."""

from __future__ import annotations

from jobmail.ingest.apple_mbox import AppleMboxSource
from jobmail.ingest.base import MailSource
from jobmail.ingest.gmail import GmailSource
from jobmail.ingest.outlook import OutlookSource

_SOURCES: dict[str, MailSource] = {
    "apple": AppleMboxSource(),
    "gmail": GmailSource(),
    "outlook": OutlookSource(),
}


def get_source(name: str) -> MailSource:
    key = name.strip().lower()
    if key not in _SOURCES:
        known = ", ".join(sorted(_SOURCES))
        raise ValueError(f"unknown mail source {name!r}; try one of: {known}")
    return _SOURCES[key]


def list_sources() -> list[str]:
    return sorted(_SOURCES)
