"""Fetch job ads and pull skills/pay via Gemini.

Flow: httpx first → if blocked, Playwright with persistent cookies.
Cache under data/job_ads/{sha1}.json:

  extracted / inactive / blocked / no_url  -> terminal
  retry                                    -> try again next run
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from bs4 import BeautifulSoup
from pydantic import BaseModel, Field

from jobmail.browser_enrich import (
    BrowserFetchError,
    BrowserSession,
    HostRegistry,
    browser_skip_hosts_from_env,
    fetch_with_browser,
    host_of,
    looks_blocked,
    should_skip_browser_host,
)
from jobmail.gemini_rate import (
    DEFAULT_MODEL,
    DEFAULT_RPM,
    GeminiModelPool,
    GeminiRateLimiter,
    compute_retry_wait,
    should_switch_model,
)
from jobmail.models import ENRICH_TERMINAL, Application
from jobmail.normalize import normalize_skills

logger = logging.getLogger(__name__)
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
FETCH_TIMEOUT = 20.0
MAX_AD_CHARS = 12_000


class EnrichResult(BaseModel):
    """Structured Gemini extract from a job-ad page."""

    posting_active: bool = True
    job_ad_title: str | None = None
    job_ad_company: str | None = None
    salary_min: float | None = None
    salary_max: float | None = None
    currency: str | None = None
    salary_period: str | None = Field(
        default=None,
        description="year|month|hour|unknown",
    )
    salary_text: str | None = None
    skills: list[str] = Field(default_factory=list)
    location: str | None = None
    employment_type: str | None = None
    seniority: str | None = None
    remote_policy: str | None = None
    notes: str = ""


def enrich_applications(
    applications: list[Application],
    out_dir: Path,
    api_key: str | None,
    *,
    skip: bool = False,
    skip_browser: bool = False,
    force_reauth_hosts: set[str] | None = None,
    model: str = DEFAULT_MODEL,
    rpm: float = DEFAULT_RPM,
    rate_limiter: GeminiRateLimiter | None = None,
    pool: GeminiModelPool | None = None,
    show_progress: bool = False,
) -> list[Application]:
    """Mutate applications in place with enrich fields; return same list."""
    out_dir = Path(out_dir)
    cache_dir = out_dir / "job_ads"
    cache_dir.mkdir(parents=True, exist_ok=True)

    if skip:
        logger.info("enrich skipped (--skip-enrich)")
        for app in applications:
            if not app.job_url:
                app.enrich_status = "no_url"
        return applications

    if not api_key:
        logger.warning("no GEMINI_API_KEY — enrich will only mark status, not extract")

    if pool is None:
        pool = GeminiModelPool(models=[model], rpm=rpm)
        if rate_limiter is not None:
            pool._states[model].limiter = rate_limiter
    client = _GeminiEnricher(api_key, pool) if api_key else None

    iterator = applications
    progress = None
    if show_progress:
        from tqdm import tqdm

        progress = tqdm(applications, desc="Enriching", unit="app")
        iterator = progress

    # one Chromium for the whole enrich pass (lazy-launches on first need)
    browser: BrowserSession | None = None
    if not skip_browser:
        browser = BrowserSession(
            out_dir,
            force_reauth_hosts=force_reauth_hosts,
            interactive=True,
        )

    try:
        for app in iterator:
            if progress is not None:
                progress.set_postfix_str(
                    (app.company or "")[:28], refresh=False
                )
            _enrich_one(
                app,
                out_dir,
                cache_dir,
                client,
                skip_browser=skip_browser,
                force_reauth_hosts=force_reauth_hosts,
                browser=browser,
            )
    finally:
        if browser is not None:
            browser.close()
        if progress is not None:
            progress.close()

    return applications


def _enrich_one(
    app: Application,
    out_dir: Path,
    cache_dir: Path,
    client: _GeminiEnricher | None,
    *,
    skip_browser: bool,
    force_reauth_hosts: set[str] | None,
    browser: BrowserSession | None = None,
) -> None:
    if not app.job_url:
        app.enrich_status = "no_url"
        return

    cache_path = cache_dir / f"{_url_hash(app.job_url)}.json"
    cached = _load_cache(cache_path)

    if cached and cached.get("status") in ENRICH_TERMINAL:
        logger.debug(
            "enrich cache hit %s (%s) for %s",
            cached.get("status"),
            app.company,
            app.job_url[:80],
        )
        _apply_cache(app, cached)
        return

    logger.info("fetching job ad for %s | %s", app.company, app.job_url[:100])
    final_url, html, via = _fetch_page(
        app.job_url,
        out_dir,
        skip_browser=skip_browser,
        force_reauth_hosts=force_reauth_hosts,
        browser=browser,
    )

    if via == "blocked":
        record = {
            "status": "blocked",
            "url": app.job_url,
            "final_url": final_url,
            "error": "bot_or_login_wall",
            "attempted_at": _now(),
        }
        _write_cache(cache_path, record)
        app.enrich_status = "blocked"
        return

    if via == "error":
        record = {
            "status": "retry",
            "url": app.job_url,
            "final_url": final_url,
            "error": html,  # error message stuffed here
            "attempted_at": _now(),
        }
        _write_cache(cache_path, record)
        app.enrich_status = "retry"
        return

    text = _html_to_text(html)
    if not text or len(text) < 40:
        # after browser still empty → blocked, don't spin forever
        status = "blocked" if via == "browser" else "retry"
        record = {
            "status": status,
            "url": app.job_url,
            "final_url": final_url,
            "error": "empty_or_short_body",
            "via": via,
            "attempted_at": _now(),
        }
        _write_cache(cache_path, record)
        app.enrich_status = status
        return

    if client is None:
        record = {
            "status": "retry",
            "url": app.job_url,
            "final_url": final_url,
            "error": "no_api_key",
            "via": via,
            "attempted_at": _now(),
            "raw_text_preview": text[:500],
        }
        _write_cache(cache_path, record)
        app.enrich_status = "retry"
        return

    try:
        extracted = client.extract(app, text, final_url or app.job_url)
    except Exception as exc:
        record = {
            "status": "retry",
            "url": app.job_url,
            "final_url": final_url,
            "error": f"gemini: {exc}",
            "via": via,
            "attempted_at": _now(),
        }
        _write_cache(cache_path, record)
        app.enrich_status = "retry"
        return

    active = bool(extracted.get("posting_active", True))
    status = "inactive" if not active else "extracted"

    record = {
        "status": status,
        "url": app.job_url,
        "final_url": final_url,
        "via": via,
        "attempted_at": _now(),
        "extracted": extracted,
    }
    _write_cache(cache_path, record)
    _apply_cache(app, record)


def _fetch_page(
    url: str,
    out_dir: Path,
    *,
    skip_browser: bool,
    force_reauth_hosts: set[str] | None,
    browser: BrowserSession | None = None,
) -> tuple[str | None, str, str]:
    """Return (final_url, html_or_error, via).

    via is one of: httpx | browser | blocked | error
    """
    try:
        final_url, html, status = _fetch_html(url)
    except Exception as exc:
        logger.warning("httpx fetch failed: %s", exc)
        final_url, html, status = None, "", None
        httpx_error = str(exc)
    else:
        httpx_error = None

    blocked = looks_blocked(html, status) if html is not None else True
    if httpx_error is None and not blocked and html:
        return final_url, html, "httpx"

    if skip_browser:
        logger.info("httpx blocked; browser enrich skipped")
        return final_url, html or httpx_error or "", "blocked"

    host = host_of(url)
    force = {h.lower() for h in (force_reauth_hosts or set())}
    registry = browser.registry if browser is not None else HostRegistry(
        Path(out_dir) / "browser_hosts.json"
    )

    # Indeed/etc: captcha loops — don't open Chrome every URL
    if should_skip_browser_host(
        host,
        force_reauth_hosts=force,
        skip_hosts=browser_skip_hosts_from_env(),
    ):
        logger.info(
            "httpx blocked; skipping browser for hard host %s "
            "(set BROWSER_SKIP_HOSTS= or --browser-reauth %s to try)",
            host,
            host,
        )
        return final_url, html or httpx_error or "browser_skip_host", "blocked"

    # prior captcha failure — don't re-open a window for every job on that host
    if registry.status(host) == "blocked" and host not in force:
        logger.info(
            "httpx blocked; %s marked blocked in browser_hosts.json — skip "
            "(--browser-reauth %s to retry)",
            host,
            host,
        )
        return final_url, html or httpx_error or "host_marked_blocked", "blocked"

    logger.info("httpx blocked — trying browser for %s", url[:100])
    try:
        b_final, b_html = fetch_with_browser(
            url,
            out_dir,
            force_reauth_hosts=force_reauth_hosts,
            interactive=True,
            session=browser,
        )
    except BrowserFetchError as exc:
        logger.warning("browser enrich failed: %s", exc)
        return final_url, str(exc), "blocked"
    except Exception as exc:
        logger.warning("browser enrich error: %s", exc)
        return final_url, str(exc), "error"

    if looks_blocked(b_html):
        return b_final, b_html, "blocked"
    return b_final, b_html, "browser"


class _GeminiEnricher:
    def __init__(self, api_key: str, pool: GeminiModelPool) -> None:
        from google import genai
        from google.genai import types

        self._types = types
        self._client = genai.Client(api_key=api_key)
        self.pool = pool

    def extract(
        self,
        app: Application,
        page_text: str,
        final_url: str,
        retries: int = 8,
    ) -> dict[str, Any]:
        if len(page_text) > MAX_AD_CHARS:
            page_text = page_text[:MAX_AD_CHARS] + "\n…[truncated]"

        prompt = f"""Extract structured job-ad fields from this posting page.
Company hint: {app.company}
Role hint: {app.role_title}
Final URL: {final_url}

If the posting says it is closed / no longer active / Stelle ist offline,
set posting_active=false but still extract salary/skills if the text remains.

Page text:
{page_text}
"""
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
                        response_schema=EnrichResult,
                        temperature=0.1,
                    ),
                )
                self.pool.commit(model)
                logger.debug("enrich ok via %s", model)
                return EnrichResult.model_validate_json(
                    (response.text or "").strip()
                ).model_dump()
            except Exception as exc:
                last_exc = exc
                self.pool.fail(model, exc)
                if should_switch_model(exc) and self.pool.has_capacity():
                    logger.warning(
                        "gemini enrich attempt %s on %s failed: %s — "
                        "switching model",
                        attempt,
                        model,
                        exc,
                    )
                    continue
                wait = compute_retry_wait(exc, attempt)
                logger.warning(
                    "gemini enrich attempt %s on %s failed: %s — "
                    "retry in %.0fs",
                    attempt,
                    model,
                    exc,
                    wait,
                )
                if wait > 0:
                    time.sleep(wait)
        raise RuntimeError(f"gemini enrich failed after retries: {last_exc}")

def _fetch_html(url: str) -> tuple[str, str, int]:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
    }
    with httpx.Client(
        follow_redirects=True,
        timeout=FETCH_TIMEOUT,
        headers=headers,
    ) as client:
        resp = client.get(url)
        if resp.status_code >= 500:
            resp.raise_for_status()
        return str(resp.url), resp.text, resp.status_code


def _html_to_text(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()
    text = soup.get_text(separator="\n")
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _url_hash(url: str) -> str:
    return hashlib.sha1(url.encode("utf-8")).hexdigest()


def _load_cache(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("bad enrich cache %s: %s", path, exc)
        return None


def _write_cache(path: Path, data: dict[str, Any]) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _apply_cache(app: Application, cached: dict[str, Any]) -> None:
    app.enrich_status = cached.get("status") or "retry"
    extracted = cached.get("extracted") or {}
    if not extracted:
        return

    if extracted.get("salary_min") is not None:
        app.salary_min = _as_float(extracted.get("salary_min"))
    if extracted.get("salary_max") is not None:
        app.salary_max = _as_float(extracted.get("salary_max"))
    if extracted.get("currency"):
        app.currency = str(extracted["currency"])
    if extracted.get("salary_period"):
        app.salary_period = str(extracted["salary_period"])
    if extracted.get("salary_text"):
        app.salary_text = str(extracted["salary_text"])
    skills = extracted.get("skills") or []
    if skills:
        seen = {s.lower() for s in app.skills}
        for skill in skills:
            s = str(skill).strip()
            if s and s.lower() not in seen:
                app.skills.append(s)
                seen.add(s.lower())
        app.skills_norm = normalize_skills(app.skills)
    if extracted.get("location"):
        app.location = str(extracted["location"])
    if extracted.get("employment_type"):
        app.employment_type = str(extracted["employment_type"])
    if extracted.get("seniority"):
        app.seniority = str(extracted["seniority"])
    if extracted.get("remote_policy"):
        app.remote_policy = str(extracted["remote_policy"])
    notes = extracted.get("notes") or ""
    if notes:
        app.notes = notes


def _as_float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _now() -> str:
    return datetime.now(UTC).isoformat()


def is_probably_tracking_url(url: str) -> bool:
    from urllib.parse import urlparse

    host = urlparse(url).netloc.lower()
    markers = (
        "click.stepstone",
        "email.stepstone",
        "engage.indeed",
        "cts.indeed",
        "clk.indeed",
    )
    return any(m in host for m in markers)
