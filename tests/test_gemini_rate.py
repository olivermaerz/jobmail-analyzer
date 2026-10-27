"""Unit tests for Gemini RPM/RPD pool + 429 retryDelay parsing."""

from __future__ import annotations

from pathlib import Path

from jobmail.gemini_rate import (
    DEFAULT_MODEL,
    DEFAULT_MODELS,
    DEFAULT_RPD,
    DEFAULT_RPM,
    GeminiModelPool,
    GeminiRateLimiter,
    compute_retry_wait,
    is_daily_quota_error,
    is_rate_limit_error,
    parse_retry_delay_seconds,
    should_switch_model,
)


def test_defaults() -> None:
    assert DEFAULT_RPM == 15
    assert DEFAULT_RPD == 500
    assert DEFAULT_MODELS == (
        "gemini-3.5-flash-lite",
        "gemini-3.1-flash-lite",
    )
    assert DEFAULT_MODEL == "gemini-3.5-flash-lite"


def test_limiter_interval() -> None:
    limiter = GeminiRateLimiter(15)
    assert abs(limiter.min_interval - 4.0) < 1e-9


def test_parse_retry_delay_structured() -> None:
    exc = RuntimeError(
        "429 RESOURCE_EXHAUSTED. {'error': {'details': "
        "[{'@type': 'type.googleapis.com/google.rpc.RetryInfo', "
        "'retryDelay': '51s'}]}}"
    )
    assert parse_retry_delay_seconds(exc) == 51.0


def test_parse_retry_delay_prose() -> None:
    exc = RuntimeError("Please retry in 41.03676652s.")
    assert parse_retry_delay_seconds(exc) == 41.03676652


def test_parse_retry_delay_missing() -> None:
    assert parse_retry_delay_seconds(RuntimeError("boom")) is None


def test_compute_retry_wait_honors_api_hint() -> None:
    exc = RuntimeError("429 RESOURCE_EXHAUSTED Please retry in 51s.")
    wait = compute_retry_wait(exc, attempt=1)
    assert wait == 53.0


def test_compute_retry_wait_rate_limit_without_hint() -> None:
    exc = RuntimeError("429 Too Many Requests")
    assert is_rate_limit_error(exc)
    assert compute_retry_wait(exc, attempt=1) == 60.0


def test_compute_retry_wait_non_rate_limit_grows() -> None:
    wait = compute_retry_wait(RuntimeError("weird"), attempt=3)
    assert wait == 8.0


def test_is_daily_quota_error() -> None:
    exc = RuntimeError(
        "429 Quota exceeded for metric: "
        "generativelanguage.googleapis.com/"
        "generate_content_requests_per_model_per_day"
    )
    assert is_daily_quota_error(exc)


def test_pool_round_robins(tmp_path: Path) -> None:
    pool = GeminiModelPool(
        models=["m-a", "m-b"],
        rpm=10_000,  # no RPM waiting in tests
        rpd=500,
        state_path=tmp_path / "quota.json",
    )
    first = pool.acquire()
    pool.commit(first)
    second = pool.acquire()
    pool.commit(second)
    assert {first, second} == {"m-a", "m-b"}


def test_pool_skips_rpd_exhausted(tmp_path: Path) -> None:
    pool = GeminiModelPool(
        models=["m-a", "m-b"],
        rpm=10_000,
        rpd=2,
        state_path=tmp_path / "quota.json",
    )
    a = pool.acquire()
    pool.fail(
        a,
        RuntimeError(
            "429 generate_content_requests_per_model_per_day exceeded"
        ),
    )
    assert pool._states[a].rpd_exhausted
    b = pool.acquire()
    assert b != a
    pool.commit(b)


def test_pool_persists_counts(tmp_path: Path) -> None:
    path = tmp_path / "quota.json"
    pool = GeminiModelPool(
        models=["m-a"],
        rpm=10_000,
        rpd=500,
        state_path=path,
    )
    m = pool.acquire()
    pool.commit(m)
    pool2 = GeminiModelPool(
        models=["m-a"],
        rpm=10_000,
        rpd=500,
        state_path=path,
    )
    assert pool2._states["m-a"].used_today == 1


def test_pool_cooldown_on_rpm_429(tmp_path: Path) -> None:
    pool = GeminiModelPool(
        models=["m-a", "m-b"],
        rpm=10_000,
        rpd=500,
        state_path=tmp_path / "quota.json",
    )
    a = pool.acquire()
    pool.fail(a, RuntimeError("429 Please retry in 30s."))
    assert pool._states[a].cooldown_until > 0
    b = pool.acquire()
    assert b != a


def test_pool_switches_on_503_high_demand(tmp_path: Path) -> None:
    pool = GeminiModelPool(
        models=["m-a", "m-b"],
        rpm=10_000,
        rpd=500,
        state_path=tmp_path / "quota.json",
    )
    a = pool.acquire()
    exc = RuntimeError(
        "503 UNAVAILABLE. This model is currently experiencing high demand."
    )
    assert should_switch_model(exc)
    before = pool._states[a].used_today
    pool.fail(a, exc)
    # 503 shouldn't burn RPD
    assert pool._states[a].used_today == before
    assert pool._states[a].cooldown_until > 0
    b = pool.acquire()
    assert b != a
