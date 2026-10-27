"""Outlook export ingest — stub.

Expected someday: .pst/.olm or an Outlook mbox/eml dump. Same contract
as AppleMboxSource — just normalize into RawEmail.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from jobmail.models import RawEmail


class OutlookSource:
    """Placeholder until we add Outlook support."""

    name = "outlook"

    def iter_emails(self, path: Path) -> Iterator[RawEmail]:
        raise NotImplementedError(
            "outlook ingest isn't built yet. Export via Apple Mail for now, "
            f"or implement OutlookSource.iter_emails for {path}."
        )
