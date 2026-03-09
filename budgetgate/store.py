"""Storage backends for BudgetGate."""

from __future__ import annotations

import secrets
import threading
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Protocol
from uuid import uuid4

from .core import Budget, Ledger

if TYPE_CHECKING:
    from redis import Redis


@dataclass(slots=True)
class SpendEvent:
    """A recorded spend."""
    ts: float
    amount: Decimal


@dataclass(slots=True)
class Reservation:
    """A pending spend reservation."""
    id: str
    ledger: Ledger
    ts: float
    amount: Decimal


class Store(Protocol):
    """Protocol for spend storage backends."""

    def check_and_reserve(
        self,
        ledger: Ledger,
        now: float,
        amount: Decimal,
        budget: Budget,
    ) -> tuple[Decimal, bool]:
        """Atomically check budget and reserve spend if allowed.

        Returns:
            (total_spent_in_window, allowed)

        If allowed=True, the spend is already reserved.
        If allowed=False, no reservation was made.
        """
        ...

    def reserve(
        self,
        ledger: Ledger,
        now: float,
        amount: Decimal,
        budget: Budget,
    ) -> tuple[str | None, Decimal]:
        """Reserve spend without committing.

        Used for bounded dynamic costs: reserve estimate, commit actual.

        Returns:
            (reservation_id, total_spent_in_window)
            reservation_id is None if budget would be exceeded.
        """
        ...

    def commit(
        self,
        reservation_id: str,
        actual: Decimal,
    ) -> None:
        """Commit a reservation with actual spend.

        Adjusts the reserved amount to actual (may be less than reserved).
        Raises KeyError if reservation_id not found.
        """
        ...

    def release(self, reservation_id: str) -> None:
        """Release a reservation without committing (e.g., on failure).

        Raises KeyError if reservation_id not found.
        """
        ...

    def get_spend(
        self,
        ledger: Ledger,
        now: float,
        window: float | None,
    ) -> Decimal:
        """Get current spend in window (read-only)."""
        ...

    def clear(self, ledger: Ledger) -> None:
        """Clear all spend history for a ledger."""
        ...

    def clear_all(self) -> None:
        """Clear all spend history."""
        ...


class MemoryStore:
    """Thread-safe in-memory spend store with reservation support.

    Lock ordering (must always acquire in this order to prevent deadlock):
        1. _global_lock
        2. ledger-specific lock from _locks

    All methods that need both locks acquire global first, then ledger.
    """

    __slots__ = ("_ledgers", "_reservations", "_locks", "_global_lock")

    def __init__(self) -> None:
        self._ledgers: dict[Ledger, list[SpendEvent]] = {}
        self._reservations: dict[str, Reservation] = {}
        self._locks: dict[Ledger, threading.Lock] = {}
        self._global_lock = threading.Lock()

    def _get_lock(self, ledger: Ledger) -> threading.Lock:
        """Get or create lock for ledger. Must hold _global_lock when calling."""
        if ledger not in self._locks:
            self._locks[ledger] = threading.Lock()
        return self._locks[ledger]

    def _prune(
        self,
        events: list[SpendEvent],
        now: float,
        window: float | None,
    ) -> list[SpendEvent]:
        """Remove events outside the window."""
        if window is None:
            return list(events)
        cutoff = now - window
        return [e for e in events if e.ts >= cutoff]

    def _get_reserved_locked(self, ledger: Ledger, now: float, window: float | None) -> Decimal:
        """Sum of active reservations for ledger within window.

        Must hold _global_lock when calling (to safely iterate _reservations).
        """
        total = Decimal("0")
        cutoff = now - window if window else None
        for res in self._reservations.values():
            if res.ledger == ledger:
                if cutoff is None or res.ts >= cutoff:
                    total += res.amount
        return total

    def check_and_reserve(
        self,
        ledger: Ledger,
        now: float,
        amount: Decimal,
        budget: Budget,
    ) -> tuple[Decimal, bool]:
        with self._global_lock:
            lock = self._get_lock(ledger)
            with lock:
                events = self._ledgers.get(ledger, [])
                pruned = self._prune(events, now, budget.window)
                committed = sum((e.amount for e in pruned), Decimal("0"))
                reserved = self._get_reserved_locked(ledger, now, budget.window)
                current_spend = committed + reserved
                if current_spend + amount > budget.max_spend:
                    self._ledgers[ledger] = pruned
                    return current_spend, False
                pruned.append(SpendEvent(ts=now, amount=amount))
                self._ledgers[ledger] = pruned
                return current_spend + amount, True

    def reserve(
        self,
        ledger: Ledger,
        now: float,
        amount: Decimal,
        budget: Budget,
    ) -> tuple[str | None, Decimal]:
        with self._global_lock:
            lock = self._get_lock(ledger)
            with lock:
                events = self._ledgers.get(ledger, [])
                pruned = self._prune(events, now, budget.window)
                self._ledgers[ledger] = pruned
                committed = sum((e.amount for e in pruned), Decimal("0"))
                reserved = self._get_reserved_locked(ledger, now, budget.window)
                current_spend = committed + reserved
                if current_spend + amount > budget.max_spend:
                    return None, current_spend
                res_id = uuid4().hex
                self._reservations[res_id] = Reservation(
                    id=res_id, ledger=ledger, ts=now, amount=amount,
                )
                return res_id, current_spend + amount

    def commit(self, reservation_id: str, actual: Decimal) -> None:
        with self._global_lock:
            if reservation_id not in self._reservations:
                raise KeyError(f"Reservation not found: {reservation_id}")
            res = self._reservations.pop(reservation_id)
            lock = self._get_lock(res.ledger)
            with lock:
                events = self._ledgers.get(res.ledger, [])
                events.append(SpendEvent(ts=res.ts, amount=actual))
                self._ledgers[res.ledger] = events

    def release(self, reservation_id: str) -> None:
        with self._global_lock:
            if reservation_id not in self._reservations:
                raise KeyError(f"Reservation not found: {reservation_id}")
            del self._reservations[reservation_id]

    def get_spend(
        self,
        ledger: Ledger,
        now: float,
        window: float | None,
    ) -> Decimal:
        with self._global_lock:
            lock = self._get_lock(ledger)
            with lock:
                events = self._ledgers.get(ledger, [])
                pruned = self._prune(events, now, window)
                committed = sum((e.amount for e in pruned), Decimal("0"))
                reserved = self._get_reserved_locked(ledger, now, window)
                return committed + reserved

    def clear(self, ledger: Ledger) -> None:
        with self._global_lock:
            lock = self._get_lock(ledger)
            with lock:
                self._ledgers.pop(ledger, None)
                to_remove = [
                    rid for rid, res in self._reservations.items()
                    if res.ledger == ledger
                ]
                for rid in to_remove:
                    del self._reservations[rid]

    def clear_all(self) -> None:
        with self._global_lock:
            self._ledgers.clear()
            self._reservations.clear()
            self._locks.clear()


# ─────────────────────────────────────────────────────────────────
# Redis Store (Lua-based atomic operations)
# ─────────────────────────────────────────────────────────────────

# Data model:
#   {prefix}:{ns}:{res}:{principal}:spends   ZSET  score=ts, member="{ts}:{nonce}:{amount}"
#   {prefix}:{ns}:{res}:{principal}:res       HASH  field=res_id, value="{ts}:{amount}"
#   {prefix}:resmap:{res_id}                  STRING value="{ns}:{res}:{principal}" (reverse index)

# Shared Lua helper: sum committed spends from ZSET members.
# Member format: "{ts}:{nonce}:{amount}" — amount is the 3rd colon-separated field.
_LUA_SUM_HELPER = """
local function sum_spends(spends_key)
    local total = 0
    local members = redis.call('ZRANGE', spends_key, 0, -1)
    for i = 1, #members do
        local s = members[i]
        local last_colon = #s
        while last_colon > 0 and string.byte(s, last_colon) ~= 58 do
            last_colon = last_colon - 1
        end
        total = total + tonumber(string.sub(s, last_colon + 1))
    end
    return total
end

local function sum_reservations(res_key, now, window)
    local total = 0
    local all_res = redis.call('HGETALL', res_key)
    for i = 1, #all_res, 2 do
        local val = all_res[i + 1]
        local colon = string.find(val, ":")
        local res_ts = tonumber(string.sub(val, 1, colon - 1))
        local res_amt = tonumber(string.sub(val, colon + 1))
        if window ~= "none" and res_ts < (now - tonumber(window)) then
            redis.call('HDEL', res_key, all_res[i])
        else
            total = total + res_amt
        end
    end
    return total
end
"""

_LUA_CHECK_AND_RESERVE = _LUA_SUM_HELPER + """
local spends_key = KEYS[1]
local res_key = KEYS[2]
local now = tonumber(ARGV[1])
local amount = tonumber(ARGV[2])
local max_spend = tonumber(ARGV[3])
local window = ARGV[4]
local member = ARGV[5]

if window ~= "none" then
    redis.call('ZREMRANGEBYSCORE', spends_key, '-inf', now - tonumber(window))
end

local committed = sum_spends(spends_key)
local reserved = sum_reservations(res_key, now, window)
local current = committed + reserved

if current + amount > max_spend then
    return {tostring(current), "0"}
end

redis.call('ZADD', spends_key, now, member)

if window ~= "none" then
    local ttl = math.ceil(tonumber(window) * 1.5)
    redis.call('EXPIRE', spends_key, ttl)
    redis.call('EXPIRE', res_key, ttl)
end

return {tostring(current + amount), "1"}
"""

_LUA_RESERVE = _LUA_SUM_HELPER + """
local spends_key = KEYS[1]
local res_key = KEYS[2]
local resmap_key = KEYS[3]
local now = tonumber(ARGV[1])
local amount = tonumber(ARGV[2])
local max_spend = tonumber(ARGV[3])
local window = ARGV[4]
local res_id = ARGV[5]
local ledger_path = ARGV[6]

if window ~= "none" then
    redis.call('ZREMRANGEBYSCORE', spends_key, '-inf', now - tonumber(window))
end

local committed = sum_spends(spends_key)
local reserved = sum_reservations(res_key, now, window)
local current = committed + reserved

if current + amount > max_spend then
    return {"", tostring(current)}
end

redis.call('HSET', res_key, res_id, now .. ":" .. tostring(amount))
redis.call('SET', resmap_key, ledger_path)

if window ~= "none" then
    local ttl = math.ceil(tonumber(window) * 1.5)
    redis.call('EXPIRE', spends_key, ttl)
    redis.call('EXPIRE', res_key, ttl)
    redis.call('EXPIRE', resmap_key, ttl)
end

return {res_id, tostring(current + amount)}
"""

_LUA_COMMIT = """
local spends_key = KEYS[1]
local res_key = KEYS[2]
local resmap_key = KEYS[3]
local res_id = ARGV[1]
local actual = ARGV[2]
local member = ARGV[3]

local res_val = redis.call('HGET', res_key, res_id)
if not res_val then
    return redis.error_reply("Reservation not found: " .. res_id)
end

local colon = string.find(res_val, ":")
local ts = tonumber(string.sub(res_val, 1, colon - 1))

redis.call('HDEL', res_key, res_id)
redis.call('DEL', resmap_key)
redis.call('ZADD', spends_key, ts, member)

return 1
"""

_LUA_GET_SPEND = _LUA_SUM_HELPER + """
local spends_key = KEYS[1]
local res_key = KEYS[2]
local now = tonumber(ARGV[1])
local window = ARGV[2]

if window ~= "none" then
    redis.call('ZREMRANGEBYSCORE', spends_key, '-inf', now - tonumber(window))
end

local committed = sum_spends(spends_key)
local reserved = sum_reservations(res_key, now, window)

return tostring(committed + reserved)
"""


class RedisStore:
    """Redis-backed spend store using Lua scripts for atomic operations.

    Suitable for distributed deployments where multiple processes or
    services share budget constraints. Requires redis-py client.

    Data model:
        - ZSET per ledger for committed spends:
          key = "{prefix}:{ns}:{res}:{principal}:spends"
          score = timestamp, member = "{ts}:{nonce}:{amount}"
        - HASH per ledger for active reservations:
          key = "{prefix}:{ns}:{res}:{principal}:res"
          field = reservation_id, value = "{ts}:{amount}"
        - STRING per reservation for reverse lookup:
          key = "{prefix}:resmap:{reservation_id}"
          value = "{ns}:{res}:{principal}" (ledger path)

    Example:
        import redis
        from budgetgate import Engine, RedisStore

        client = redis.Redis(host='localhost', port=6379, decode_responses=True)
        engine = Engine(store=RedisStore(client))
    """

    __slots__ = ("_client", "_prefix", "_s_check", "_s_reserve",
                 "_s_commit", "_s_spend")

    def __init__(self, client: "Redis", prefix: str = "budgetgate") -> None:
        self._client = client
        self._prefix = prefix
        self._s_check = client.register_script(_LUA_CHECK_AND_RESERVE)
        self._s_reserve = client.register_script(_LUA_RESERVE)
        self._s_commit = client.register_script(_LUA_COMMIT)
        self._s_spend = client.register_script(_LUA_GET_SPEND)

    def _ledger_path(self, ledger: Ledger) -> str:
        return f"{ledger.namespace}:{ledger.resource}:{ledger.principal}"

    def _spends_key(self, ledger: Ledger) -> str:
        return f"{self._prefix}:{self._ledger_path(ledger)}:spends"

    def _res_key(self, ledger: Ledger) -> str:
        return f"{self._prefix}:{self._ledger_path(ledger)}:res"

    def _resmap_key(self, res_id: str) -> str:
        return f"{self._prefix}:resmap:{res_id}"

    def check_and_reserve(
        self,
        ledger: Ledger,
        now: float,
        amount: Decimal,
        budget: Budget,
    ) -> tuple[Decimal, bool]:
        nonce = secrets.token_hex(4)
        member = f"{now}:{nonce}:{amount}"
        window_arg = str(budget.window) if budget.window is not None else "none"

        result = self._s_check(
            keys=[self._spends_key(ledger), self._res_key(ledger)],
            args=[str(now), str(amount), str(budget.max_spend), window_arg, member],
        )

        total = Decimal(result[0].decode() if isinstance(result[0], bytes) else result[0])
        allowed = (result[1].decode() if isinstance(result[1], bytes) else result[1]) == "1"
        return total, allowed

    def reserve(
        self,
        ledger: Ledger,
        now: float,
        amount: Decimal,
        budget: Budget,
    ) -> tuple[str | None, Decimal]:
        res_id = uuid4().hex
        window_arg = str(budget.window) if budget.window is not None else "none"

        result = self._s_reserve(
            keys=[self._spends_key(ledger), self._res_key(ledger),
                  self._resmap_key(res_id)],
            args=[str(now), str(amount), str(budget.max_spend), window_arg,
                  res_id, self._ledger_path(ledger)],
        )

        returned_id = result[0].decode() if isinstance(result[0], bytes) else result[0]
        total = Decimal(result[1].decode() if isinstance(result[1], bytes) else result[1])

        if returned_id == "":
            return None, total
        return returned_id, total

    def commit(self, reservation_id: str, actual: Decimal) -> None:
        resmap_key = self._resmap_key(reservation_id)
        ledger_path = self._client.get(resmap_key)
        if ledger_path is None:
            raise KeyError(f"Reservation not found: {reservation_id}")

        if isinstance(ledger_path, bytes):
            ledger_path = ledger_path.decode()

        spends_key = f"{self._prefix}:{ledger_path}:spends"
        res_key = f"{self._prefix}:{ledger_path}:res"

        # Get timestamp from reservation for the spend member
        res_val = self._client.hget(res_key, reservation_id)
        if res_val is None:
            raise KeyError(f"Reservation not found: {reservation_id}")
        if isinstance(res_val, bytes):
            res_val = res_val.decode()
        ts = res_val.split(":")[0]

        nonce = secrets.token_hex(4)
        member = f"{ts}:{nonce}:{actual}"

        self._s_commit(
            keys=[spends_key, res_key, resmap_key],
            args=[reservation_id, str(actual), member],
        )

    def release(self, reservation_id: str) -> None:
        resmap_key = self._resmap_key(reservation_id)
        ledger_path = self._client.get(resmap_key)
        if ledger_path is None:
            raise KeyError(f"Reservation not found: {reservation_id}")

        if isinstance(ledger_path, bytes):
            ledger_path = ledger_path.decode()

        res_key = f"{self._prefix}:{ledger_path}:res"
        self._client.hdel(res_key, reservation_id)
        self._client.delete(resmap_key)

    def get_spend(
        self,
        ledger: Ledger,
        now: float,
        window: float | None,
    ) -> Decimal:
        window_arg = str(window) if window is not None else "none"
        result = self._s_spend(
            keys=[self._spends_key(ledger), self._res_key(ledger)],
            args=[str(now), window_arg],
        )
        val = result.decode() if isinstance(result, bytes) else result
        return Decimal(val)

    def clear(self, ledger: Ledger) -> None:
        self._client.delete(
            self._spends_key(ledger),
            self._res_key(ledger),
        )

    def clear_all(self) -> None:
        """Clear all budgetgate keys. Warning: Uses SCAN."""
        pattern = f"{self._prefix}:*"
        cursor = 0
        while True:
            cursor, keys = self._client.scan(cursor, match=pattern, count=100)
            if keys:
                self._client.delete(*keys)
            if cursor == 0:
                break
