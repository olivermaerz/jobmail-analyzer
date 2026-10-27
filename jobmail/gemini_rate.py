"""Pace Gemini calls across one or more models.

Free tier quotas are usually per-model (RPM + RPD). Round-robin across
models roughly doubles throughput; we also park a model on 429 for the
API's retryDelay, and stop using it for the day when RPD is spent.

RPD day boundary follows America/Los_Angeles (Google AI Studio's clock).
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

DEFAULT_RPM = 15
DEFAULT_RPD = 500
DEFAULT_MODELS = (
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
)
# keep a single-model default for older call sites / --model help text
DEFAULT_MODEL = DEFAULT_MODELS[0]
_PACIFIC = ZoneInfo("America/Los_Angeles")


class GeminiRateLimiter:
    """Min-interval limiter based on requests-per-minute (one model)."""

    def __init__(self, rpm: float = DEFAULT_RPM) -> None:
        self.rpm = max(float(rpm), 0.1)
        self.min_interval = 60.0 / self.rpm
        self._last_end = 0.0

    def wait_turn(self) -> None:
        now = time.monotonic()
        gap = self.min_interval - (now - self._last_end)
        if gap > 0:
            logger.debug("rpm pacing: sleeping %.1fs (rpm=%s)", gap, self.rpm)
            time.sleep(gap)

    def mark_done(self) -> None:
        self._last_end = time.monotonic()

    @property
    def ready_in(self) -> float:
        now = time.monotonic()
        return max(0.0, self.min_interval - (now - self._last_end))


@dataclass
class _ModelState:
    model: str
    limiter: GeminiRateLimiter
    used_today: int = 0
    cooldown_until: float = 0.0  # monotonic
    rpd_exhausted: bool = False


@dataclass
class GeminiModelPool:
    """Pick a free model under per-model RPM + RPD caps."""

    models: list[str]
    rpm: float = DEFAULT_RPM
    rpd: int = DEFAULT_RPD
    state_path: Path | None = None
    _states: dict[str, _ModelState] = field(default_factory=dict, init=False)
    _day: str = field(default="", init=False)
    _rr: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        if not self.models:
            raise ValueError("GeminiModelPool needs at least one model")
        self.rpm = max(float(self.rpm), 0.1)
        self.rpd = max(int(self.rpd), 1)
        self.models = [m.strip() for m in self.models if m.strip()]
        for name in self.models:
            self._states[name] = _ModelState(
                model=name,
                limiter=GeminiRateLimiter(self.rpm),
            )
        self._day = _pacific_day()
        self._load()

    @classmethod
    def from_env(
        cls,
        *,
        state_path: Path | None = None,
        model_override: str | None = None,
    ) -> GeminiModelPool:
        """Build from GEMINI_MODELS / GEMINI_RPM / GEMINI_RPD.

        model_override (CLI --model) forces a single-model pool.
        """
        import os

        if model_override:
            models = [model_override.strip()]
        else:
            raw = os.getenv("GEMINI_MODELS", "").strip()
            if raw:
                models = [m.strip() for m in raw.split(",") if m.strip()]
            else:
                models = list(DEFAULT_MODELS)

        rpm = _env_float("GEMINI_RPM", DEFAULT_RPM)
        rpd = int(_env_float("GEMINI_RPD", float(DEFAULT_RPD)))
        return cls(models=models, rpm=rpm, rpd=rpd, state_path=state_path)

    def summary(self) -> str:
        self._roll_day()
        parts = []
        for name in self.models:
            st = self._states[name]
            parts.append(f"{name} {st.used_today}/{self.rpd} rpd")
        return (
            f"models={len(self.models)} rpm={self.rpm}/model "
            + "; ".join(parts)
        )

    def acquire(self) -> str:
        """Block until a model is under RPM+RPD; return its id."""
        while True:
            self._roll_day()
            now = time.monotonic()
            ready: list[_ModelState] = []
            soonest = None
            any_not_exhausted = False

            for name in self.models:
                st = self._states[name]
                if st.used_today >= self.rpd or st.rpd_exhausted:
                    continue
                any_not_exhausted = True
                if st.cooldown_until > now:
                    wait = st.cooldown_until - now
                    soonest = wait if soonest is None else min(soonest, wait)
                    continue
                rpm_wait = st.limiter.ready_in
                if rpm_wait > 0:
                    soonest = (
                        rpm_wait if soonest is None else min(soonest, rpm_wait)
                    )
                    continue
                ready.append(st)

            if ready:
                # prefer fewer RPD used today; round-robin among ties
                min_used = min(s.used_today for s in ready)
                tied = [s for s in ready if s.used_today == min_used]
                pick = tied[self._rr % len(tied)]
                self._rr += 1
                pick.limiter.wait_turn()
                logger.debug("gemini acquire %s", pick.model)
                return pick.model

            if not any_not_exhausted:
                raise RuntimeError(
                    "all Gemini models hit GEMINI_RPD for today — "
                    f"{self.summary()}"
                )

            sleep_for = max(0.5, soonest or 1.0)
            logger.info(
                "all models busy/cooling — sleep %.1fs (%s)",
                sleep_for,
                self.summary(),
            )
            time.sleep(sleep_for)

    def commit(self, model: str) -> None:
        """Record a finished API call (success). Counts against RPD."""
        st = self._states[model]
        st.limiter.mark_done()
        st.used_today += 1
        if st.used_today >= self.rpd:
            st.rpd_exhausted = True
            logger.warning(
                "%s reached local RPD cap (%s)", model, self.rpd
            )
        self._save()

    def fail(self, model: str, exc: BaseException) -> None:
        """Record a failed API call; park the model when useful.

        Quota errors burn RPD. Plain 503/high-demand does not — those
        usually didn't consume the daily budget — but we cool the model
        so the pool flips to a sibling.
        """
        st = self._states[model]
        st.limiter.mark_done()

        if is_daily_quota_error(exc):
            st.used_today = max(st.used_today + 1, self.rpd)
            st.rpd_exhausted = True
            logger.warning("%s marked RPD-exhausted by API: %s", model, exc)
        elif is_rate_limit_error(exc):
            st.used_today += 1
            wait = compute_retry_wait(exc, attempt=1)
            st.cooldown_until = time.monotonic() + wait
            logger.warning(
                "%s cooling %.0fs after rate limit", model, wait
            )
        elif is_transient_error(exc):
            wait = compute_retry_wait(exc, attempt=1)
            # high demand: short park, try the other model immediately
            wait = max(wait, 20.0)
            st.cooldown_until = time.monotonic() + wait
            logger.warning(
                "%s cooling %.0fs after transient/high-demand", model, wait
            )
        else:
            st.used_today += 1
        self._save()

    def has_capacity(self) -> bool:
        self._roll_day()
        return any(
            not st.rpd_exhausted and st.used_today < self.rpd
            for st in self._states.values()
        )

    def _roll_day(self) -> None:
        today = _pacific_day()
        if today == self._day:
            return
        logger.info(
            "RPD day rolled %s -> %s — resetting counters", self._day, today
        )
        self._day = today
        for st in self._states.values():
            st.used_today = 0
            st.rpd_exhausted = False
        self._save()

    def _load(self) -> None:
        if not self.state_path or not self.state_path.exists():
            return
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("bad gemini quota state %s: %s", self.state_path, exc)
            return
        if data.get("day") != self._day:
            return
        counts = data.get("counts") or {}
        exhausted = set(data.get("rpd_exhausted") or [])
        for name, st in self._states.items():
            st.used_today = int(counts.get(name, 0))
            st.rpd_exhausted = name in exhausted or st.used_today >= self.rpd

    def _save(self) -> None:
        if not self.state_path:
            return
        payload: dict[str, Any] = {
            "day": self._day,
            "counts": {
                name: st.used_today for name, st in self._states.items()
            },
            "rpd_exhausted": [
                name
                for name, st in self._states.items()
                if st.rpd_exhausted
            ],
            "rpm": self.rpm,
            "rpd": self.rpd,
        }
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(
            json.dumps(payload, indent=2) + "\n",
            encoding="utf-8",
        )


def parse_models(raw: str | None) -> list[str]:
    if not raw or not raw.strip():
        return list(DEFAULT_MODELS)
    return [m.strip() for m in raw.split(",") if m.strip()]


def parse_retry_delay_seconds(exc: BaseException) -> float | None:
    """Pull retry delay from a Gemini 429 / RESOURCE_EXHAUSTED error."""
    text = str(exc)

    match = re.search(
        r"retryDelay['\"\s:=]+['\"]?(\d+(?:\.\d+)?)\s*s",
        text,
        re.IGNORECASE,
    )
    if match:
        return float(match.group(1))

    match = re.search(
        r"retry in\s+(\d+(?:\.\d+)?)\s*s",
        text,
        re.IGNORECASE,
    )
    if match:
        return float(match.group(1))

    return None


def is_rate_limit_error(exc: BaseException) -> bool:
    text = str(exc).lower()
    return (
        "429" in text
        or "resource_exhausted" in text
        or "rate limit" in text
        or "quota exceeded" in text
        or "too many requests" in text
    )


def is_daily_quota_error(exc: BaseException) -> bool:
    """True when the API says this model's daily quota is gone."""
    text = str(exc).lower().replace("-", "_").replace(" ", "")
    markers = (
        "per_day",
        "perday",
        "requests_per_day",
        "requestsperday",
        "dailyquota",
        "generaterequestsperday",
    )
    return any(m in text for m in markers)


def is_transient_error(exc: BaseException) -> bool:
    text = str(exc).lower()
    return (
        "503" in text
        or "unavailable" in text
        or "high demand" in text
        or "timeout" in text
    )


def should_switch_model(exc: BaseException) -> bool:
    """Errors where another model in the pool is likely healthier right now."""
    return (
        is_rate_limit_error(exc)
        or is_daily_quota_error(exc)
        or is_transient_error(exc)
    )


def compute_retry_wait(exc: BaseException, attempt: int) -> float:
    """Seconds to sleep before the next attempt on the *same* model."""
    if is_daily_quota_error(exc):
        return 0.0  # switch models; don't busy-wait
    if is_rate_limit_error(exc):
        hinted = parse_retry_delay_seconds(exc)
        base = (hinted + 2.0) if hinted is not None else 60.0
        return min(180.0, max(base, 15.0))
    if is_transient_error(exc):
        return min(90.0, 5.0 * attempt)
    return min(30.0, float(2**attempt))


def _pacific_day() -> str:
    return datetime.now(_PACIFIC).date().isoformat()


def _env_float(name: str, default: float) -> float:
    import os

    raw = os.getenv(name, str(default)).strip()
    try:
        return max(float(raw), 0.1)
    except ValueError:
        logger.warning("invalid %s=%r — using %s", name, raw, default)
        return float(default)
