"""Async tests for BudgetGate."""

from decimal import Decimal

import pytest

from budgetgate import (
    AsyncMemoryStore,
    BlockReason,
    Budget,
    BudgetExceededError,
    Decision,
    Engine,
    Ledger,
    Mode,
    Status,
    StoreErrorMode,
)

# ═══════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════


class MockClock:
    """Controllable clock for testing."""

    def __init__(self, start: float = 0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class AsyncFailingStore:
    """Async store that always raises."""

    async def check_and_reserve(self, ledger, now, amount, budget):
        raise ConnectionError("Store unavailable")

    async def reserve(self, ledger, now, amount, budget):
        raise ConnectionError("Store unavailable")

    async def commit(self, reservation_id, actual):
        raise ConnectionError("Store unavailable")

    async def release(self, reservation_id):
        raise ConnectionError("Store unavailable")

    async def get_spend(self, ledger, now, window):
        raise ConnectionError("Store unavailable")

    async def clear(self, ledger):
        raise ConnectionError("Store unavailable")

    async def clear_all(self):
        raise ConnectionError("Store unavailable")


# ═══════════════════════════════════════════════════════════════
# async_check
# ═══════════════════════════════════════════════════════════════


class TestAsyncCheck:
    """async_check mirrors sync check behavior."""

    @pytest.mark.asyncio
    async def test_allows_within_budget(self):
        engine = Engine(async_store=AsyncMemoryStore())
        ledger = Ledger("openai", "gpt-4", "user:1")
        budget = Budget(max_spend=Decimal("10.00"), window=60.0)

        d = await engine.async_check(ledger, Decimal("3.00"), budget)
        assert d.allowed
        assert d.spent_in_window == Decimal("3.00")
        assert d.remaining == Decimal("7.00")

    @pytest.mark.asyncio
    async def test_blocks_over_budget(self):
        engine = Engine(async_store=AsyncMemoryStore())
        ledger = Ledger("openai", "gpt-4", "user:1")
        budget = Budget(max_spend=Decimal("10.00"), window=60.0)

        await engine.async_check(ledger, Decimal("8.00"), budget)
        d = await engine.async_check(ledger, Decimal("3.00"), budget)

        assert d.blocked
        assert d.reason == BlockReason.BUDGET_EXCEEDED

    @pytest.mark.asyncio
    async def test_window_expiry_resets_spend(self):
        clock = MockClock(1000)
        engine = Engine(clock=clock, async_store=AsyncMemoryStore())
        ledger = Ledger("openai", "gpt-4", "user:1")
        budget = Budget(max_spend=Decimal("10.00"), window=60.0)

        await engine.async_check(ledger, Decimal("9.00"), budget)
        clock.advance(70)
        d = await engine.async_check(ledger, Decimal("5.00"), budget)
        assert d.allowed

    @pytest.mark.asyncio
    async def test_different_principals_independent(self):
        engine = Engine(async_store=AsyncMemoryStore())
        budget = Budget(max_spend=Decimal("10.00"), window=60.0)

        l1 = Ledger("openai", "gpt-4", "user:A")
        l2 = Ledger("openai", "gpt-4", "user:B")

        await engine.async_check(l1, Decimal("9.00"), budget)
        d = await engine.async_check(l2, Decimal("9.00"), budget)
        assert d.allowed

    @pytest.mark.asyncio
    async def test_unbounded_window(self):
        engine = Engine(async_store=AsyncMemoryStore())
        ledger = Ledger("openai", "gpt-4", "user:1")
        budget = Budget(max_spend=Decimal("100.00"), window=None)

        await engine.async_check(ledger, Decimal("50.00"), budget)
        d = await engine.async_check(ledger, Decimal("51.00"), budget)
        assert d.blocked


# ═══════════════════════════════════════════════════════════════
# async_enforce
# ═══════════════════════════════════════════════════════════════


class TestAsyncEnforce:
    """async_enforce mirrors sync enforce."""

    @pytest.mark.asyncio
    async def test_hard_mode_raises(self):
        engine = Engine(async_store=AsyncMemoryStore())
        ledger = Ledger("openai", "gpt-4", "user:1")
        budget = Budget(max_spend=Decimal("5.00"), window=60.0, mode=Mode.HARD)

        await engine.async_check(ledger, Decimal("5.00"), budget)
        decision = await engine.async_check(ledger, Decimal("1.00"), budget)

        with pytest.raises(BudgetExceededError) as exc:
            await engine.async_enforce(decision)
        assert exc.value.decision.reason == BlockReason.BUDGET_EXCEEDED

    @pytest.mark.asyncio
    async def test_soft_mode_no_exception(self):
        engine = Engine(async_store=AsyncMemoryStore())
        ledger = Ledger("openai", "gpt-4", "user:1")
        budget = Budget(max_spend=Decimal("5.00"), window=60.0, mode=Mode.SOFT)

        await engine.async_check(ledger, Decimal("5.00"), budget)
        decision = await engine.async_check(ledger, Decimal("1.00"), budget)

        await engine.async_enforce(decision)  # Should not raise
        assert decision.blocked


# ═══════════════════════════════════════════════════════════════
# async_guard
# ═══════════════════════════════════════════════════════════════


class TestAsyncGuard:
    """@engine.async_guard decorator."""

    @pytest.mark.asyncio
    async def test_returns_value_directly(self):
        engine = Engine(async_store=AsyncMemoryStore())
        ledger = Ledger("openai", "gpt-4", "user:1")
        budget = Budget(max_spend=Decimal("10.00"), window=60.0)

        @engine.async_guard(ledger, budget, cost=Decimal("1.00"))
        async def query(prompt: str) -> str:
            return f"response to {prompt}"

        result = await query("hello")
        assert result == "response to hello"

    @pytest.mark.asyncio
    async def test_raises_on_budget_exceeded(self):
        engine = Engine(async_store=AsyncMemoryStore())
        ledger = Ledger("openai", "gpt-4", "user:1")
        budget = Budget(max_spend=Decimal("2.00"), window=60.0)

        @engine.async_guard(ledger, budget, cost=Decimal("1.50"))
        async def query() -> str:
            return "ok"

        assert await query() == "ok"
        with pytest.raises(BudgetExceededError):
            await query()

    @pytest.mark.asyncio
    async def test_preserves_function_metadata(self):
        engine = Engine(async_store=AsyncMemoryStore())

        @engine.async_guard(
            Ledger("ns", "res"), Budget(max_spend=Decimal("100")),
            cost=Decimal("1"),
        )
        async def my_func():
            """My docstring."""

        assert my_func.__name__ == "my_func"
        assert my_func.__doc__ == "My docstring."


# ═══════════════════════════════════════════════════════════════
# async_guard_result
# ═══════════════════════════════════════════════════════════════


class TestAsyncGuardResult:
    """@engine.async_guard_result decorator."""

    @pytest.mark.asyncio
    async def test_returns_result_wrapper(self):
        engine = Engine(async_store=AsyncMemoryStore())
        ledger = Ledger("openai", "gpt-4", "user:1")
        budget = Budget(max_spend=Decimal("10.00"), window=60.0)

        @engine.async_guard_result(ledger, budget, cost=Decimal("1.00"))
        async def query(prompt: str) -> str:
            return f"response to {prompt}"

        result = await query("hello")
        assert result.ok
        assert result.value == "response to hello"

    @pytest.mark.asyncio
    async def test_blocked_returns_missing(self):
        engine = Engine(async_store=AsyncMemoryStore())
        ledger = Ledger("openai", "gpt-4", "user:1")
        budget = Budget(max_spend=Decimal("2.00"), window=60.0, mode=Mode.SOFT)

        @engine.async_guard_result(ledger, budget, cost=Decimal("1.50"))
        async def query() -> str:
            return "ok"

        assert (await query()).value == "ok"
        result = await query()

        assert not result.ok
        assert not result.has_value
        assert result.value is None

    @pytest.mark.asyncio
    async def test_none_return_not_confused_with_blocked(self):
        engine = Engine(async_store=AsyncMemoryStore())

        @engine.async_guard_result(
            Ledger("ns", "res"),
            Budget(max_spend=Decimal("100")),
            cost=Decimal("1"),
        )
        async def void_op() -> None:
            return None

        result = await void_op()
        assert result.ok is True
        assert result.has_value is True
        assert result.value is None
        assert result.unwrap() is None

    @pytest.mark.asyncio
    async def test_unwrap_or_default(self):
        engine = Engine(async_store=AsyncMemoryStore())
        ledger = Ledger("openai", "gpt-4", "user:1")
        budget = Budget(max_spend=Decimal("2.00"), window=60.0, mode=Mode.SOFT)

        @engine.async_guard_result(ledger, budget, cost=Decimal("1.50"))
        async def query() -> int:
            return 42

        await query()
        assert (await query()).unwrap_or(0) == 0


# ═══════════════════════════════════════════════════════════════
# Async store error modes
# ═══════════════════════════════════════════════════════════════


class TestAsyncStoreErrorModes:
    """Async store failure handling."""

    @pytest.mark.asyncio
    async def test_fail_closed_blocks_on_error(self):
        engine = Engine(async_store=AsyncFailingStore())
        ledger = Ledger("openai", "gpt-4", "user:1")
        budget = Budget(
            max_spend=Decimal("10.00"), on_store_error=StoreErrorMode.FAIL_CLOSED,
        )

        decision = await engine.async_check(ledger, Decimal("1.00"), budget)

        assert decision.blocked
        assert decision.reason == BlockReason.STORE_ERROR
        assert "fail-closed" in decision.message

    @pytest.mark.asyncio
    async def test_fail_open_allows_on_error(self):
        engine = Engine(async_store=AsyncFailingStore())
        ledger = Ledger("openai", "gpt-4", "user:1")
        budget = Budget(
            max_spend=Decimal("10.00"), on_store_error=StoreErrorMode.FAIL_OPEN,
        )

        decision = await engine.async_check(ledger, Decimal("1.00"), budget)

        assert decision.allowed
        assert decision.reason == BlockReason.STORE_ERROR
        assert "fail-open" in decision.message


# ═══════════════════════════════════════════════════════════════
# Async listeners
# ═══════════════════════════════════════════════════════════════


class TestAsyncListeners:
    """Async decisions still emit to listeners."""

    @pytest.mark.asyncio
    async def test_listener_receives_async_decisions(self):
        decisions: list[Decision] = []
        engine = Engine(async_store=AsyncMemoryStore())
        engine.on_decision(decisions.append)

        ledger = Ledger("openai", "gpt-4", "user:1")
        budget = Budget(max_spend=Decimal("10.00"), window=60.0)

        await engine.async_check(ledger, Decimal("1.00"), budget)
        await engine.async_check(ledger, Decimal("2.00"), budget)

        assert len(decisions) == 2
        assert all(d.status == Status.ALLOW for d in decisions)
