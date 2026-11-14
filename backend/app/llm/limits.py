# Rate-limit tracking for every model, so requests go only to models with budget left. Pure
# bookkeeping with an injectable clock (unit-tested with a fake one in tests/test_limits.py); the
# router feeds it and persists the daily counts (db/llm_usage.py) through `on_record`.
#   - Per minute: a 60-second sliding window of requests and tokens.
#   - Per day: requests and tokens per UTC date, loaded from Mongo at startup so restarts don't
#     reset them.
#   - Reservations: a request holds its estimated tokens from before it starts until it finishes
#     (then its actual count replaces the estimate), so concurrent calls can't overspend.
#   - Rate-limit headers: when a provider reports less budget left than we've counted (other
#     clients, or our estimates were low), our counts are raised to match. Never lowered.
#   - Cooldowns: a 429 parks the model until its reset time. Repeated server or connection errors
#     sideline the whole provider for a few minutes.
import re
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

WINDOW = 60.0  # seconds in the per-minute window
HOUR = 3600.0  # seconds in the per-hour window (audio seconds for speech-to-text)
DEFAULT_COOLDOWN = 60.0  # a 429 that doesn't say when to retry
MAX_COOLDOWN = 24 * 3600.0
PROVIDER_ERRORS = 3  # consecutive server/connection errors that sideline a provider
SIDELINE_SECONDS = 300.0


class SystemClock:
    def now(self) -> float:
        return time.time()


def day_of(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d")


_DURATION = re.compile(r"(\d+(?:\.\d+)?)(ms|h|m|s)")


def parse_duration(text: str | None) -> float | None:
    """Seconds from "7.66s", "2m59.56s", "1h2m3s", "120ms" or a plain number; None if unreadable."""
    if text is None:
        return None
    text = str(text).strip()
    try:
        return float(text)
    except ValueError:
        pass
    parts = _DURATION.findall(text)
    if not parts or "".join(n + u for n, u in parts) != text.replace(" ", ""):
        return None
    scale = {"h": 3600, "m": 60, "s": 1, "ms": 0.001}
    return sum(float(n) * scale[u] for n, u in parts)


@dataclass
class Reservation:
    model: str
    started: float
    tokens: int
    entry: list  # [time, tokens] inside the model's minute window, updated when the request finishes
    audio: float = 0.0  # seconds of audio (speech-to-text), estimated until the request finishes
    audio_entry: list | None = None  # [time, seconds] inside the hour window
    done: bool = False


@dataclass
class ModelState:
    minute: deque = field(default_factory=deque)  # [time, tokens] per request
    day: str = ""
    day_requests: int = 0
    day_tokens: int = 0
    hour_audio: deque = field(default_factory=deque)  # [time, audio seconds] per transcription
    day_audio: float = 0.0
    cooldown_until: float = 0.0
    cooldown_reason: str = ""


@dataclass
class ProviderState:
    errors: int = 0
    sidelined_until: float = 0.0


class Limiter:
    """`limits`: model key -> {"rpm", "tpm", "rpd", "tpd", "ash", "asd"} (None/missing = unlimited;
    ash/asd are audio seconds per hour/day, for speech-to-text models).
    `providers`: model key -> provider name. `header_scopes`: provider -> {"requests": "minute"|"day",
    "tokens": "minute"|"day"}, saying what its x-ratelimit-* headers count (no entry = ignore them).
    `on_record(model, day, requests, tokens, audio_seconds)` is called for each finished request (for
    persistence)."""

    def __init__(self, limits: dict[str, dict], providers: dict[str, str], header_scopes: dict | None = None,
                 clock=None, on_record: Callable | None = None):
        self.limits = limits
        self.providers = providers
        self.header_scopes = header_scopes or {}
        self.clock = clock or SystemClock()
        self.on_record = on_record
        self.models = {m: ModelState() for m in limits}
        self.provider_state = {p: ProviderState() for p in set(providers.values())}

    # --- state helpers ---

    def _state(self, model: str) -> ModelState:
        st = self.models[model]
        now = self.clock.now()
        while st.minute and st.minute[0][0] <= now - WINDOW:
            st.minute.popleft()
        while st.hour_audio and st.hour_audio[0][0] <= now - HOUR:
            st.hour_audio.popleft()
        today = day_of(now)
        if st.day != today:
            st.day, st.day_requests, st.day_tokens, st.day_audio = today, 0, 0, 0.0
        return st

    def load_day(self, day: str, counts: dict[str, dict]) -> None:
        """Restores today's totals (from Mongo at startup): model -> {requests, tokens, audio_seconds}."""
        for model, c in counts.items():
            if model in self.models:
                st = self._state(model)
                if st.day == day:
                    st.day_requests = max(st.day_requests, c.get("requests", 0))
                    st.day_tokens = max(st.day_tokens, c.get("tokens", 0))
                    st.day_audio = max(st.day_audio, c.get("audio_seconds", 0.0))

    # --- budgets ---

    def remaining(self, model: str) -> dict:
        """Budget left now: requests and tokens per minute and per day, and audio seconds per hour and
        per day (None = unlimited)."""
        st = self._state(model)
        lim = self.limits[model]
        left = lambda limit, used: None if limit is None else max(0, limit - used)
        return {
            "minute_requests": left(lim.get("rpm"), len(st.minute)),
            "minute_tokens": left(lim.get("tpm"), sum(t for _, t in st.minute)),
            "day_requests": left(lim.get("rpd"), st.day_requests),
            "day_tokens": left(lim.get("tpd"), st.day_tokens),
            "hour_audio": left(lim.get("ash"), round(sum(a for _, a in st.hour_audio), 1)),
            "day_audio": left(lim.get("asd"), round(st.day_audio, 1)),
        }

    def blocked_for(self, model: str) -> float:
        """Seconds until the model is off cooldown and its provider isn't sidelined (0 = available)."""
        now = self.clock.now()
        st = self.models[model]
        provider = self.provider_state[self.providers[model]]
        return max(0.0, st.cooldown_until - now, provider.sidelined_until - now)

    def check(self, model: str, tokens: int, audio: float = 0.0) -> tuple[bool, str, float]:
        """Whether a request of about `tokens` (and `audio` seconds of speech) fits now: (ok, reason if
        not, seconds until it might)."""
        if wait := self.blocked_for(model):
            now, st = self.clock.now(), self.models[model]
            reason = st.cooldown_reason if st.cooldown_until > now else "provider sidelined after repeated errors"
            return False, reason, wait
        left = self.remaining(model)
        st = self._state(model)
        now = self.clock.now()
        frees_up = (st.minute[0][0] + WINDOW - now) if st.minute else 0.0
        tomorrow = (int(now // 86400) + 1) * 86400 - now
        if left["day_requests"] is not None and left["day_requests"] < 1:
            return False, "daily requests used up", tomorrow
        if left["day_tokens"] is not None and left["day_tokens"] < tokens:
            return False, "daily tokens used up", tomorrow
        if left["minute_requests"] is not None and left["minute_requests"] < 1:
            return False, "per-minute requests used up", frees_up
        if left["minute_tokens"] is not None and left["minute_tokens"] < tokens:
            tpm = self.limits[model].get("tpm")
            if tokens > tpm:  # bigger than a whole minute's budget: it can never fit
                return False, f"request (~{tokens} tokens) exceeds the {tpm}/minute limit", float("inf")
            return False, "per-minute tokens used up", self._frees_in(st.minute, tokens, tpm, now, WINDOW)
        if audio:
            if left["day_audio"] is not None and left["day_audio"] < audio:
                return False, "daily audio seconds used up", tomorrow
            if left["hour_audio"] is not None and left["hour_audio"] < audio:
                ash = self.limits[model].get("ash")
                if audio > ash:
                    return False, f"recording (~{audio:.0f}s) exceeds the {ash}s/hour limit", float("inf")
                return False, "hourly audio seconds used up", self._frees_in(st.hour_audio, audio, ash, now, HOUR)
        return True, "", 0.0

    @staticmethod
    def _frees_in(window: deque, amount: float, limit: float, now: float, span: float) -> float:
        """Seconds until enough of a sliding window ages out for `amount` to fit under `limit`."""
        used = sum(n for _, n in window)
        for t, n in window:  # oldest first
            used -= n
            if limit - used >= amount:
                return max(0.0, t + span - now)
        return span

    # --- requests ---

    def reserve(self, model: str, tokens: int, audio: float = 0.0) -> Reservation:
        st = self._state(model)
        now = self.clock.now()
        entry = [now, tokens]
        st.minute.append(entry)
        st.day_requests += 1
        st.day_tokens += tokens
        audio_entry = None
        if audio:
            audio_entry = [now, audio]
            st.hour_audio.append(audio_entry)
            st.day_audio += audio
        return Reservation(model, now, tokens, entry, audio, audio_entry)

    def finish(self, res: Reservation, actual_tokens: int | None, actual_audio: float | None = None) -> None:
        """Replaces the reservation's estimates with the actual counts (keeps an estimate if the
        provider reported none)."""
        if res.done:
            return
        res.done = True
        st = self._state(res.model)
        actual = actual_tokens if actual_tokens else res.tokens
        res.entry[1] = actual
        audio = actual_audio if actual_audio is not None else res.audio
        if res.audio_entry is not None:
            res.audio_entry[1] = audio
        elif audio:
            st.hour_audio.append([res.started, audio])
        if day_of(res.started) == st.day:
            st.day_tokens += actual - res.tokens
            st.day_audio += audio - res.audio
        provider = self.provider_state[self.providers[res.model]]
        provider.errors = 0
        if self.on_record:
            self.on_record(res.model, day_of(res.started), 1, actual, audio)

    def release(self, res: Reservation) -> None:
        """A request that failed before using anything (e.g. a 429): undo its reservation."""
        if res.done:
            return
        res.done = True
        st = self._state(res.model)
        if res.entry in st.minute:
            st.minute.remove(res.entry)
        if res.audio_entry is not None and res.audio_entry in st.hour_audio:
            st.hour_audio.remove(res.audio_entry)
        if day_of(res.started) == st.day:
            st.day_requests -= 1
            st.day_tokens -= res.tokens
            st.day_audio -= res.audio

    # --- provider feedback ---

    def sync_headers(self, model: str, headers) -> None:
        """Raises our counts to match the provider's reported remaining budget, if it says less."""
        scopes = self.header_scopes.get(self.providers[model])
        if not scopes or headers is None:
            return
        st = self._state(model)
        lim = self.limits[model]
        now = self.clock.now()
        for kind in ("requests", "tokens"):
            scope = scopes.get(kind)
            reported = headers.get(f"x-ratelimit-remaining-{kind}")
            try:
                reported = int(float(reported))
            except (TypeError, ValueError):
                continue
            if scope == "minute":
                limit = lim.get("rpm" if kind == "requests" else "tpm")
                if limit is None:
                    continue
                used = len(st.minute) if kind == "requests" else sum(t for _, t in st.minute)
                missing = (limit - reported) - used  # used elsewhere, or our estimates were low
                if missing > 0:
                    st.minute.append([now, missing if kind == "tokens" else 0])
                    if kind == "requests":
                        for _ in range(missing - 1):
                            st.minute.append([now, 0])
            elif scope == "day":
                limit = lim.get("rpd" if kind == "requests" else "tpd")
                if limit is None:
                    continue
                if kind == "requests":
                    st.day_requests = max(st.day_requests, limit - reported)
                else:
                    st.day_tokens = max(st.day_tokens, limit - reported)

    def rate_limited(self, model: str, headers=None, message: str = "") -> float:
        """A 429: cools the model down until its reset time. Returns the cooldown in seconds."""
        seconds = None
        if headers is not None:
            seconds = parse_duration(headers.get("retry-after"))
            if seconds is None:
                resets = [parse_duration(headers.get(f"x-ratelimit-reset-{k}")) for k in ("requests", "tokens")]
                resets = [r for r in resets if r]
                seconds = max(resets) if resets else None
        if seconds is None and (m := re.search(r"try again in ((?:\d+(?:\.\d+)?(?:ms|h|m|s))+)", message)):
            seconds = parse_duration(m.group(1))
        seconds = min(seconds if seconds is not None else DEFAULT_COOLDOWN, MAX_COOLDOWN)
        st = self.models[model]
        st.cooldown_until = max(st.cooldown_until, self.clock.now() + seconds)
        st.cooldown_reason = "rate-limited (429)"
        return seconds

    def provider_error(self, model: str) -> bool:
        """A server or connection error. Returns True if this sidelined the provider."""
        provider = self.provider_state[self.providers[model]]
        provider.errors += 1
        if provider.errors >= PROVIDER_ERRORS:
            provider.errors = 0
            provider.sidelined_until = self.clock.now() + SIDELINE_SECONDS
            return True
        return False
