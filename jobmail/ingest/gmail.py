"""Gmail export ingest — stub.

Expected someday: a Gmail Takeout mbox or .eml folder. Wire it up as a
MailSource that yields RawEmail; registry entry is already reserved.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from jobmail.models import RawEmail


class GmailSource:
    """Placeholder until we add Takeout support."""

    name = "gmail"

    def iter_emails(self, path: Path) -> Iterator[RawEmail]:
        raise NotImplementedError(
            "gmail ingest isn't built yet. Export via Apple Mail for now, "
            f"or implement GmailSource.iter_emails for {path}."
        )
