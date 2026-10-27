"""Playwright fallback when httpx gets Cloudflare'd / login-walled.

Uses a persistent Chrome profile under data/browser_profile/ so cookies
survive across runs. Enrich keeps one BrowserSession open for the whole
pass (no open/close per URL). Tries headless first; upgrades to a visible
window only when a page still looks blocked / login-walled.

Host registry (browser_hosts.json) remembers which sites you've unlocked.
Indeed is skipped by default — captcha loops.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

HOSTS_FILE = "browser_hosts.json"
PROFILE_DIR = "browser_profile"

# Playwright + Cloudflare/Indeed captcha loops forever — skip browser for these
# unless --browser-reauth or BROWSER_SKIP_HOSTS= (empty) overrides.
# Match is suffix on host (indeed.com covers de.indeed.com).
DEFAULT_BROWSER_SKIP_HOSTS = ("indeed.com",)

_BLOCK_MARKERS = (
    "cf-mitigated",
    "challenge-platform",
    "attention required",
    "just a moment",
    "enable javascript and cookies",
    "access denied",
    "captcha-delivery",
    "cf-browser-verification",
)
_LOGIN_MARKERS = (
    "sign in",
    "log in",
    "anmelden",
    "einloggen",
)


class BrowserFetchError(RuntimeError):
    """Browser could not get usable page text."""


def host_of(url: str) -> str:
    host = urlparse(url).netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    return host


def looks_blocked(html: str, status_code: int | None = None) -> bool:
    """True when the response is a bot wall / challenge, not a job ad."""
    if status_code in {401, 403}:
        return True
    low = (html or "").lower()
    if not low.strip():
        return True
    if any(m in low for m in _BLOCK_MARKERS):
        return True
    # Cloudflare often shows up in headers mirrored into body snippets
    if "cloudflare" in low and len(low) < 12000:
        return True
    return len(re.sub(r"\s+", "", low)) < 80


def looks_like_login(html: str) -> bool:
    low = (html or "").lower()
    if not low:
        return False
    hits = sum(1 for m in _LOGIN_MARKERS if m in low)
    return hits >= 2 and len(low) < 25000


def needs_human_page(html: str) -> bool:
    return looks_blocked(html) or looks_like_login(html)


def browser_skip_hosts_from_env() -> tuple[str, ...]:
    """Hosts (suffixes) where we don't open Playwright after httpx fails."""
    import os

    if "BROWSER_SKIP_HOSTS" in os.environ:
        raw = os.environ["BROWSER_SKIP_HOSTS"].strip()
        if not raw:
            return ()
        return tuple(h.strip().lower() for h in raw.split(",") if h.strip())
    return DEFAULT_BROWSER_SKIP_HOSTS


def host_matches(host: str, suffixes: tuple[str, ...] | list[str]) -> bool:
    host = host.lower()
    for suffix in suffixes:
        s = suffix.lower().lstrip(".")
        if host == s or host.endswith("." + s):
            return True
    return False


def should_skip_browser_host(
    host: str,
    *,
    force_reauth_hosts: set[str] | None = None,
    skip_hosts: tuple[str, ...] | None = None,
) -> bool:
    """True → mark blocked, don't launch Chromium."""
    force = {h.lower() for h in (force_reauth_hosts or set())}
    if host.lower() in force or host_matches(host, tuple(force)):
        return False
    suffixes = (
        DEFAULT_BROWSER_SKIP_HOSTS if skip_hosts is None else tuple(skip_hosts)
    )
    return host_matches(host, suffixes)


class HostRegistry:
    """Remember which hosts we've unlocked via manual login."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._data: dict[str, dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            self._data = {}
            return
        try:
            self._data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("bad browser_hosts.json: %s", exc)
            self._data = {}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self._data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def status(self, host: str) -> str | None:
        entry = self._data.get(host) or {}
        return entry.get("status")

    def mark(self, host: str, status: str, *, notes: str = "") -> None:
        entry = self._data.get(host) or {}
        entry["status"] = status
        entry["notes"] = notes
        now = datetime.now(UTC).isoformat()
        if status == "logged_in":
            entry["last_success_at"] = now
        else:
            entry["last_attempt_at"] = now
        self._data[host] = entry
        self.save()

    def clear(self, host: str | None = None) -> None:
        if host is None:
            self._data = {}
        else:
            self._data.pop(host, None)
        self.save()


class BrowserSession:
    """One Chromium for the whole enrich pass — navigate, don't relaunch.

    Starts headless. If a page needs a human, upgrades to headed (same
    profile) and stays headed for the rest of the session.
    """

    def __init__(
        self,
        out_dir: Path,
        *,
        force_reauth_hosts: set[str] | None = None,
        interactive: bool = True,
    ) -> None:
        self.out_dir = Path(out_dir)
        self.profile = self.out_dir / PROFILE_DIR
        self.profile.mkdir(parents=True, exist_ok=True)
        self.registry = HostRegistry(self.out_dir / HOSTS_FILE)
        self.force_reauth_hosts = {
            h.lower() for h in (force_reauth_hosts or set())
        }
        self.interactive = interactive

        self._playwright = None
        self._context = None
        self._page = None
        self._headless: bool | None = None
        # hosts we already prompted for this session (don't nag every URL)
        self._prompted: set[str] = set()

    def __enter__(self) -> BrowserSession:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        if self._context is not None:
            try:
                self._context.close()
            except Exception as exc:
                logger.debug("browser context close: %s", exc)
            self._context = None
            self._page = None
        if self._playwright is not None:
            try:
                self._playwright.stop()
            except Exception as exc:
                logger.debug("playwright stop: %s", exc)
            self._playwright = None
        self._headless = None

    def fetch(self, url: str) -> tuple[str, str]:
        """Navigate to url in the shared browser; return (final_url, html)."""
        host = host_of(url)
        force = host in self.force_reauth_hosts or host_matches(
            host, tuple(self.force_reauth_hosts)
        )

        want_headed = force or self._headless is False
        self._ensure_browser(headless=not want_headed)

        final, html = self._navigate(url)

        if not force and not needs_human_page(html):
            self.registry.mark(host, "logged_in", notes="session ok")
            return final, html

        # escalate headless → headed once, then keep that window
        if self._headless:
            logger.info(
                "headless blocked/login on %s — upgrading to visible window",
                host,
            )
            if not self.interactive:
                raise BrowserFetchError(
                    f"blocked on {host} and interactive login disabled"
                )
            self._ensure_browser(headless=False)
            final, html = self._navigate(url)

        if force or needs_human_page(html):
            if not self.interactive:
                raise BrowserFetchError(
                    f"blocked on {host} and interactive login disabled"
                )
            if host not in self._prompted or needs_human_page(html) or force:
                _prompt_login(host, self._page)
                self._prompted.add(host)
                # clear force so we don't re-prompt every URL after unlock
                self.force_reauth_hosts.discard(host)
                final, html = self._navigate(url)

        if looks_blocked(html):
            self.registry.mark(
                host,
                "blocked",
                notes="still blocked after headed login prompt",
            )
            raise BrowserFetchError(
                f"still blocked on {host} after browser attempt"
            )

        self.registry.mark(host, "logged_in", notes="session headed ok")
        return final, html

    def _ensure_browser(self, *, headless: bool) -> None:
        if self._context is not None and self._headless == headless:
            return

        # persistent profile can't be opened twice — close before relaunch
        if self._context is not None:
            try:
                self._context.close()
            except Exception as exc:
                logger.debug("browser relaunch close: %s", exc)
            self._context = None
            self._page = None

        if self._playwright is None:
            try:
                from playwright.sync_api import sync_playwright
            except ImportError as exc:
                raise BrowserFetchError(
                    "playwright not installed — "
                    "pip install playwright && playwright install chromium"
                ) from exc
            self._playwright = sync_playwright().start()

        mode = "headless" if headless else "headed"
        logger.info("browser launching %s (shared session)", mode)
        self._context = self._playwright.chromium.launch_persistent_context(
            user_data_dir=str(self.profile),
            headless=headless,
            viewport={"width": 1280, "height": 900},
            locale="de-DE",
            args=["--disable-blink-features=AutomationControlled"],
        )
        self._page = self._context.new_page()
        self._headless = headless

    def _navigate(self, url: str) -> tuple[str, str]:
        assert self._page is not None
        mode = "headless" if self._headless else "headed"
        logger.info("browser %s navigating %s", mode, url[:120])
        self._page.goto(url, wait_until="domcontentloaded", timeout=60000)
        self._page.wait_for_timeout(1500)
        return self._page.url, self._page.content()


def fetch_with_browser(
    url: str,
    out_dir: Path,
    *,
    force_reauth_hosts: set[str] | None = None,
    interactive: bool = True,
    session: BrowserSession | None = None,
) -> tuple[str, str]:
    """Fetch url via Chromium; return (final_url, html).

    Prefer passing a BrowserSession from enrich so the window stays open.
    Without one, opens a short-lived session for a single URL.
    """
    if session is not None:
        return session.fetch(url)

    with BrowserSession(
        out_dir,
        force_reauth_hosts=force_reauth_hosts,
        interactive=interactive,
    ) as owned:
        return owned.fetch(url)


def _prompt_login(host: str, page) -> None:
    print()
    print("=" * 60)
    print(f"Browser enrich needs you on: {host}")
    print(f"Current URL: {page.url}")
    print("Log in / pass the challenge in the browser window.")
    print("When the job page looks right, come back here and press Enter.")
    print("=" * 60)
    try:
        input("Press Enter to continue… ")
    except EOFError as exc:
        raise BrowserFetchError("no TTY for login prompt") from exc
