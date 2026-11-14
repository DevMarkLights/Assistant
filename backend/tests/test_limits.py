# Unit tests for the rate limiter (app/llm/limits.py), driven by a fake clock. From backend/:
#   ../.venv/bin/python -m pytest tests
import math

import pytest

from app.llm.limits import Limiter, day_of, parse_duration

DAY = 86400.0


class FakeClock:
    def __init__(self, t: float = 1_800_000_000.0):  # a fixed moment, mid-day UTC
        self.t = t

    def now(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def make(limits=None, header_scopes=None, records=None):
    clock = FakeClock()
    limits = limits or {"big": {"rpm": 30, "tpm": 8000, "rpd": 1000, "tpd": 200000},
                        "small": {"rpm": 30, "tpm": 8000, "rpd": 1000, "tpd": 200000}}
    providers = {m: "groq" for m in limits}
    on_record = (lambda *args: records.append(args)) if records is not None else None
    return Limiter(limits, providers, header_scopes, clock, on_record), clock


def use(limiter, model, tokens, actual=None):
    limiter.finish(limiter.reserve(model, tokens), actual if actual is not None else tokens)


# --- per-minute window ---

def test_tokens_per_minute_slide_out_after_60_seconds():
    lim, clock = make({"m": {"tpm": 1000}})
    use(lim, "m", 600)
    clock.advance(20)
    ok, reason, wait = lim.check("m", 500)
    assert not ok and reason == "per-minute tokens used up"
    assert wait == pytest.approx(40)  # the 600 ages out 60s after it was used
    clock.advance(40.1)
    assert lim.check("m", 500)[0]
    assert lim.remaining("m")["minute_tokens"] == 1000


def test_requests_per_minute():
    lim, clock = make({"m": {"rpm": 2}})
    use(lim, "m", 10)
    clock.advance(1)
    use(lim, "m", 10)
    ok, reason, wait = lim.check("m", 10)
    assert not ok and reason == "per-minute requests used up" and wait == pytest.approx(59)
    clock.advance(59.1)
    assert lim.check("m", 10)[0]


def test_request_bigger_than_a_whole_minute_never_fits():
    lim, _ = make({"m": {"tpm": 1000}})
    ok, reason, wait = lim.check("m", 1500)
    assert not ok and "exceeds" in reason and math.isinf(wait)


def test_unlimited_when_no_limits_given():
    lim, _ = make({"m": {}})
    for _ in range(100):
        use(lim, "m", 10_000)
    assert lim.check("m", 10_000) == (True, "", 0.0)


# --- reservations ---

def test_actual_tokens_replace_the_estimate():
    lim, _ = make({"m": {"tpm": 1000, "tpd": 5000}})
    res = lim.reserve("m", 800)
    assert lim.remaining("m")["minute_tokens"] == 200  # held while in flight
    lim.finish(res, 100)
    assert lim.remaining("m")["minute_tokens"] == 900
    assert lim.remaining("m")["day_tokens"] == 4900


def test_concurrent_reservations_cannot_overspend():
    lim, _ = make({"m": {"tpm": 1000}})
    lim.reserve("m", 600)
    assert not lim.check("m", 600)[0]  # the first is still in flight


def test_release_undoes_a_failed_request():
    lim, _ = make({"m": {"tpm": 1000, "rpd": 5}})
    lim.release(lim.reserve("m", 700))
    assert lim.remaining("m") == {"minute_requests": None, "minute_tokens": 1000, "day_requests": 5, "day_tokens": None}


def test_missing_usage_keeps_the_estimate():
    lim, _ = make({"m": {"tpm": 1000}})
    lim.finish(lim.reserve("m", 300), None)
    assert lim.remaining("m")["minute_tokens"] == 700


def test_finished_requests_are_reported_for_persistence():
    records = []
    lim, clock = make({"m": {"tpm": 1000}}, records=records)
    use(lim, "m", 300, actual=250)
    assert records == [("m", day_of(clock.now()), 1, 250)]


# --- daily limits ---

def test_daily_requests_block_until_tomorrow_then_reset():
    lim, clock = make({"m": {"rpd": 3}})
    for _ in range(3):
        use(lim, "m", 10)
        clock.advance(61)
    ok, reason, wait = lim.check("m", 10)
    assert not ok and reason == "daily requests used up"
    clock.advance(wait + 1)  # past midnight UTC
    assert lim.check("m", 10)[0]
    assert lim.remaining("m")["day_requests"] == 3


def test_daily_tokens():
    lim, clock = make({"m": {"tpd": 1000}})
    use(lim, "m", 900)
    assert lim.check("m", 100)[0]
    assert lim.check("m", 101)[1] == "daily tokens used up"


def test_load_day_restores_counts_after_a_restart():
    lim, clock = make({"m": {"rpd": 1000, "tpd": 200000}})
    lim.load_day(day_of(clock.now()), {"m": (990, 150_000), "unknown-model": (1, 1)})
    assert lim.remaining("m")["day_requests"] == 10
    assert lim.remaining("m")["day_tokens"] == 50_000


def test_yesterdays_counts_are_not_loaded_as_today():
    lim, clock = make({"m": {"rpd": 1000}})
    lim.load_day(day_of(clock.now() - DAY), {"m": (990, 0)})
    assert lim.remaining("m")["day_requests"] == 1000


# --- 429 cooldowns ---

def test_429_cools_down_until_retry_after():
    lim, clock = make()
    assert lim.rate_limited("big", {"retry-after": "12"}) == 12
    ok, reason, wait = lim.check("big", 10)
    assert not ok and reason == "rate-limited (429)" and wait == pytest.approx(12)
    assert lim.check("small", 10)[0]  # other models are unaffected
    clock.advance(12.1)
    assert lim.check("big", 10)[0]


def test_429_uses_reset_headers_when_there_is_no_retry_after():
    lim, _ = make()
    headers = {"x-ratelimit-reset-requests": "2m59.56s", "x-ratelimit-reset-tokens": "7.66s"}
    assert lim.rate_limited("big", headers) == pytest.approx(179.56)


def test_429_falls_back_to_the_message_then_a_default():
    lim, _ = make()
    assert lim.rate_limited("big", {}, "Rate limit reached. Please try again in 7m38.784s.") == pytest.approx(458.784)
    assert lim.rate_limited("small", None, "rate limited") == 60


# --- provider errors ---

def test_repeated_errors_sideline_every_model_of_the_provider():
    lim, clock = make()
    assert not lim.provider_error("big")
    assert not lim.provider_error("small")
    assert lim.provider_error("big")  # third in a row
    for model in ("big", "small"):
        ok, reason, wait = lim.check(model, 10)
        assert not ok and "sidelined" in reason and wait == pytest.approx(300)
    clock.advance(300.1)
    assert lim.check("big", 10)[0]


def test_a_success_resets_the_error_count():
    lim, _ = make()
    lim.provider_error("big")
    lim.provider_error("big")
    use(lim, "big", 10)
    assert not lim.provider_error("big")
    assert lim.check("big", 10)[0]


# --- rate-limit headers ---

GROQ_SCOPES = {"groq": {"requests": "day", "tokens": "minute"}}


def test_headers_lower_our_budgets_to_match_the_provider():
    lim, clock = make(header_scopes=GROQ_SCOPES)
    use(lim, "big", 1000)  # we think 7000 tokens/minute and 999 requests/day are left
    lim.sync_headers("big", {"x-ratelimit-remaining-tokens": "5000", "x-ratelimit-remaining-requests": "900"})
    left = lim.remaining("big")
    assert left["minute_tokens"] == 5000
    assert left["day_requests"] == 900
    clock.advance(60.1)  # the extra minute usage ages out like any other
    assert lim.remaining("big")["minute_tokens"] == 8000


def test_headers_never_raise_our_budgets():
    lim, _ = make(header_scopes=GROQ_SCOPES)
    use(lim, "big", 3000)
    lim.sync_headers("big", {"x-ratelimit-remaining-tokens": "8000", "x-ratelimit-remaining-requests": "1000"})
    assert lim.remaining("big")["minute_tokens"] == 5000
    assert lim.remaining("big")["day_requests"] == 999


def test_headers_are_ignored_without_a_scope_mapping():
    lim, _ = make()
    lim.sync_headers("big", {"x-ratelimit-remaining-tokens": "10", "x-ratelimit-remaining-requests": "1"})
    assert lim.remaining("big")["minute_tokens"] == 8000
    assert lim.remaining("big")["day_requests"] == 1000


def test_garbage_headers_are_ignored():
    lim, _ = make(header_scopes=GROQ_SCOPES)
    lim.sync_headers("big", {"x-ratelimit-remaining-tokens": "lots"})
    assert lim.remaining("big")["minute_tokens"] == 8000


# --- durations ---

@pytest.mark.parametrize("text,seconds", [
    ("12", 12), ("7.66s", 7.66), ("2m59.56s", 179.56), ("1h2m3s", 3723), ("120ms", 0.12), ("0.5", 0.5),
])
def test_parse_duration(text, seconds):
    assert parse_duration(text) == pytest.approx(seconds)


@pytest.mark.parametrize("text", [None, "", "soon", "5 parsecs"])
def test_parse_duration_rejects_unreadable_values(text):
    assert parse_duration(text) is None
