"""MailSource protocol — the extension point for new mail formats."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Protocol

from jobmail.models import RawEmail


class MailSource(Protocol):
    """Anything that can turn an export path into RawEmail objects."""

    name: str

    def iter_emails(self, path: Path) -> Iterator[RawEmail]:
        """Yield normalized emails from the export at path."""
        ...
