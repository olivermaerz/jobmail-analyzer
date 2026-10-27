"""Apple Mail mbox ingest.

Apple's export is a bog-standard mbox plus a binary table_of_contents we
ignore entirely. mailbox.mbox is enough.
"""

from __future__ import annotations

import logging
import mailbox
import re
from collections.abc import Iterator
from datetime import datetime
from email.header import decode_header, make_header
from email.message import Message
from email.utils import parsedate_to_datetime
from pathlib import Path

from jobmail.models import RawEmail

logger = logging.getLogger(__name__)

_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)


class AppleMboxSource:
    """Read an Apple Mail (or any classic) mbox export."""

    name = "apple"

    def iter_emails(self, path: Path) -> Iterator[RawEmail]:
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"mbox not found: {path}")

        # companion binary next to the mbox — leave it alone
        toc = path.parent / "table_of_contents"
        if toc.exists():
            logger.debug("ignoring Apple table_of_contents at %s", toc)

        mbox = mailbox.mbox(str(path))
        try:
            for msg in mbox:
                raw = self._message_to_raw(msg)
                if raw is None:
                    continue
                yield raw
        finally:
            mbox.close()

    def _message_to_raw(self, msg: Message) -> RawEmail | None:
        message_id = (msg.get("Message-ID") or msg.get("Message-Id") or "").strip()
        if not message_id:
            logger.warning(
                "skipping mail with empty Message-ID (subject=%r)",
                msg.get("Subject"),
            )
            return None

        subject = _decode_header(msg.get("Subject"))
        from_addr = _decode_header(msg.get("From"))
        to_addr = _decode_header(msg.get("To"))
        date = _parse_date(msg.get("Date"))
        body_text, body_html = _extract_bodies(msg)
        urls = _extract_urls(body_text, body_html)

        headers = {
            "message_id": message_id,
            "date": msg.get("Date") or "",
            "from": from_addr,
            "to": to_addr,
            "subject": subject,
        }

        return RawEmail(
            message_id=message_id,
            date=date,
            from_addr=from_addr,
            to_addr=to_addr,
            subject=subject,
            body_text=body_text,
            body_html=body_html,
            urls=urls,
            source_plugin=self.name,
            headers=headers,
        )


def _decode_header(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def _parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return parsedate_to_datetime(value)
    except Exception:
        return None


def _extract_bodies(msg: Message) -> tuple[str, str]:
    """Prefer text/plain; keep html around for URL scraping."""
    plain_parts: list[str] = []
    html_parts: list[str] = []

    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_maintype() == "multipart":
                continue
            disposition = str(part.get("Content-Disposition") or "")
            if "attachment" in disposition.lower():
                continue
            ctype = part.get_content_type()
            payload = _decode_payload(part)
            if not payload:
                continue
            if ctype == "text/plain":
                plain_parts.append(payload)
            elif ctype == "text/html":
                html_parts.append(payload)
    else:
        payload = _decode_payload(msg)
        if payload:
            if msg.get_content_type() == "text/html":
                html_parts.append(payload)
            else:
                plain_parts.append(payload)

    return "\n".join(plain_parts).strip(), "\n".join(html_parts).strip()


def _decode_payload(part: Message) -> str:
    try:
        raw = part.get_payload(decode=True)
        if raw is None:
            # already-decoded string payload
            payload = part.get_payload()
            return payload if isinstance(payload, str) else ""
        charset = part.get_content_charset() or "utf-8"
        return raw.decode(charset, errors="replace")
    except Exception:
        return ""


def _extract_urls(*texts: str) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for text in texts:
        for match in _URL_RE.findall(text or ""):
            url = match.rstrip(").,;]>\"'")
            if url not in seen:
                seen.add(url)
                out.append(url)
    return out
