"""Ask Gemini what each email means for the job hunt.

One mail per request keeps retries simple. Body is truncated — we care
about the status signal, not the full legal footer.

Existing applications are passed in as a company→roles catalog so portal
mails (Stepstone/Indeed) reuse the real employer key when we already
know it.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Literal

from pydantic import BaseModel, Field

from jobmail import SCHEMA_VERSION
from jobmail.catalog import build_catalog_text, collect_catalog_keys
from jobmail.gemini_rate import (
    DEFAULT_MODEL,
    DEFAULT_RPM,
    GeminiModelPool,
    GeminiRateLimiter,
    compute_retry_wait,
    should_switch_model,
)
from jobmail.models import EVENT_TYPES, Application, Event, RawEmail
from jobmail.normalize import (
    normalize_company,
    normalize_role,
    normalize_skills,
    resolve_application_key,
)

logger = logging.getLogger(__name__)

MAX_BODY_CHARS = 10_000
MAX_URLS = 15

EventType = Literal[
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
]


class ClassifyResult(BaseModel):
    """Structured Gemini output for one email."""

    event_type: EventType
    company: str = Field(
        default="",
        description="hiring organization — NOT Stepstone/Indeed/Personio",
    )
    role_title: str = ""
    application_key: str = Field(
        default="",
        description=(
            "exact key= from catalog when this mail matches an existing app; "
            "otherwise leave empty — we derive the key from company + role"
        ),
    )
    source: str = Field(
        default="other",
        description="indeed|stepstone|personio|linkedin|workday|direct|other",
    )
    job_url: str | None = Field(
        default=None,
        description="prefer career-page URL over tracking/board click wrappers",
    )
    confidence: float = 0.0
    summary: str = ""
    salary_text: str | None = None
    skills: list[str] = Field(default_factory=list)


class Classifier:
    """Thin wrapper around the Gemini client."""

    def __init__(
        self,
        api_key: str,
        model: str = DEFAULT_MODEL,
        rpm: float = DEFAULT_RPM,
        rate_limiter: GeminiRateLimiter | None = None,
        pool: GeminiModelPool | None = None,
    ) -> None:
        from google import genai
        from google.genai import types

        self._types = types
        self._client = genai.Client(api_key=api_key)
        if pool is not None:
            self.pool = pool
        else:
            # single-model fallback for tests / ad-hoc use
            self.pool = GeminiModelPool(
                models=[model],
                rpm=rpm,
            )
            if rate_limiter is not None:
                self.pool._states[model].limiter = rate_limiter

    def classify(
        self,
        email: RawEmail,
        *,
        catalog_apps: list[Application] | None = None,
        catalog_events: list[Event] | None = None,
    ) -> Event:
        catalog = build_catalog_text(catalog_apps, catalog_events)
        catalog_keys = collect_catalog_keys(catalog_apps, catalog_events)
        prompt = _build_prompt(email, catalog)
        logger.debug(
            "classify %s | %s | %s",
            email.message_id[:48],
            email.date,
            email.subject[:80],
        )

        data = self._call_gemini(prompt)
        return _event_from_response(email, data, catalog_keys)

    def _call_gemini(self, prompt: str, retries: int = 8) -> dict:
        types = self._types
        last_exc: Exception | None = None
        for attempt in range(1, retries + 1):
            model = self.pool.acquire()
            try:
                response = self._client.models.generate_content(
                    model=model,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        response_schema=ClassifyResult,
                        temperature=0.1,
                    ),
                )
                text = (response.text or "").strip()
                self.pool.commit(model)
                logger.debug("classify ok via %s", model)
                return ClassifyResult.model_validate_json(text).model_dump()
            except Exception as exc:
                last_exc = exc
                self.pool.fail(model, exc)
                # 429 / 503 / high-demand: pool parked that model — try sibling
                if should_switch_model(exc) and self.pool.has_capacity():
                    logger.warning(
                        "gemini classify attempt %s on %s failed: %s — "
                        "switching model",
                        attempt,
                        model,
                        exc,
                    )
                    continue
                wait = compute_retry_wait(exc, attempt)
                logger.warning(
                    "gemini classify attempt %s on %s failed: %s — "
                    "retry in %.0fs",
                    attempt,
                    model,
                    exc,
                    wait,
                )
                if wait > 0:
                    time.sleep(wait)
        raise RuntimeError(f"gemini classify failed after retries: {last_exc}")


def _build_prompt(email: RawEmail, catalog: str) -> str:
    body = email.body_text or _strip_html_rough(email.body_html)
    if len(body) > MAX_BODY_CHARS:
        body = body[:MAX_BODY_CHARS] + "\n…[truncated]"

    urls = email.urls[:MAX_URLS]
    url_block = "\n".join(f"- {u}" for u in urls) if urls else "(none)"
    date_s = email.date.isoformat() if email.date else ""

    return f"""You classify job-application related emails (German or English).

Return JSON matching the schema. Rules:
- newsletter_noise: job alerts / digests / "X companies looking for you"
  — not a real application update.
- application_sent: confirmation that I applied / Bewerbung ist raus.
- application_ack: company/portal received the application.
- rejection / offer / interview_invite / interview_followup /
  assessment / update / other as appropriate.
- company = the HIRING employer. Stepstone, Indeed, LinkedIn, Personio,
  Workday are NEVER the company — put those in source.
- Prefer an existing catalog key=… when the mail is about that
  employer/role (including portal wording / aliases). Copy it exactly.
- Otherwise leave application_key empty — we derive it from company +
  role ourselves. Still fill company (HIRING employer) and role_title.
- job_url: prefer a real career-page / job posting URL. Avoid tracking
  click wrappers (click.stepstone, engage.indeed, cts.indeed) when a
  cleaner URL is available; null if only unsubscribe links.
- skills / salary_text: only if clearly present in the email.

{catalog}

From: {email.from_addr}
To: {email.to_addr}
Date: {date_s}
Subject: {email.subject}

URLs found:
{url_block}

Body:
{body}
"""


def _event_from_response(
    email: RawEmail,
    data: dict,
    catalog_keys: set[str] | None = None,
) -> Event:
    event_type = data.get("event_type") or "other"
    if event_type not in EVENT_TYPES:
        event_type = "other"

    company = (data.get("company") or "").strip()
    role = (data.get("role_title") or "").strip()
    company_norm = normalize_company(company)
    role_norm = normalize_role(role)

    app_key = resolve_application_key(
        company,
        role,
        data.get("application_key"),
        catalog_keys,
    )

    job_url = data.get("job_url") or None
    if isinstance(job_url, str):
        job_url = job_url.strip() or None

    skills = data.get("skills") or []
    if not isinstance(skills, list):
        skills = []
    skills_clean = [str(s).strip() for s in skills if str(s).strip()]
    skills_norm = normalize_skills(skills_clean)

    return Event(
        schema_version=SCHEMA_VERSION,
        message_id=email.message_id,
        date=email.date.isoformat() if email.date else None,
        event_type=event_type,
        company=company,
        role_title=role,
        application_key=app_key,
        source=(data.get("source") or "").strip().lower() or "other",
        job_url=job_url,
        confidence=float(data.get("confidence") or 0.0),
        summary=(data.get("summary") or "").strip(),
        source_plugin=email.source_plugin,
        salary_text=(data.get("salary_text") or None),
        skills=skills_clean,
        company_norm=company_norm,
        role_title_norm=role_norm,
        skills_norm=skills_norm,
        from_addr=email.from_addr,
        subject=email.subject,
    )


def _strip_html_rough(html: str) -> str:
    if not html:
        return ""
    text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", html)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()
