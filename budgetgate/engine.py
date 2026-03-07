"""Core engine for BudgetGate."""

from __future__ import annotations

import time
from decimal import Decimal
from functools import wraps
from typing import Callable, ParamSpec, TypeVar

from .core import (
    BlockReason,
    Budget,
    Decision,
    Ledger,
    Mode,
    Result,
    Status,
    StoreErrorMode,
)
from .emitter import Emitter
from .store import MemoryStore, Store

P = ParamSpec("P")
T = TypeVar("T")


class BudgetExceeded(RuntimeError):
    """Raised when spend would exceed budget in HARD mode."""

    def __init__(self, decision: Decision) -> None:
        super().__init__(decision.message or f"Budget exceeded: {decision.reason}")
        self.decision = decision


class Engine:
    """BudgetGate engine for spend limiting agent actions."""

    __slots__ = ("_store", "_clock", "_budgets", "_emitter")

    def __init__(
        self,
        store: Store | None = None,
        clock: Callable[[], float] | None = None,
        emitter: Emitter | None = None,
    ) -> None:
        self._store: Store = store or MemoryStore()
        self._clock = clock or time.monotonic
        self._budgets: dict[Ledger, Budget] = {}
        self._emitter = emitter or Emitter()

    # ─────────────────────────────────────────────────────────────
    # Configuration
    # ─────────────────────────────────────────────────────────────

    def register(self, ledger: Ledger, budget: Budget) -> None:
        self._budgets[ledger] = budget

    def budget_for(self, ledger: Ledger) -> Budget:
        return self._budgets.get(ledger, Budget(max_spend=Decimal("Infinity")))

    def on_decision(self, listener: Callable[[Decision], None]) -> None:
        self._emitter.add(listener)

    @property
    def listener_errors(self) -> int:
        return self._emitter.error_count

    # ─────────────────────────────────────────────────────────────
    # Core API
    # ─────────────────────────────────────────────────────────────

    def check(self, ledger: Ledger, amount: Decimal, budget: Budget | None = None) -> Decision:
        now = self._clock()
        budget = budget or self.budget_for(ledger)
        try:
            total_spent, allowed = self._store.check_and_reserve(ledger, now, amount, budget)
        except Exception as e:
            return self._handle_store_error(ledger, budget, amount, e)
        if allowed:
            remaining = max(Decimal("0"), budget.max_spend - total_spent)
            return self._decide(ledger, budget, amount, status=Status.ALLOW,
                                spent_in_window=total_spent, remaining=remaining)
        remaining = max(Decimal("0"), budget.max_spend - total_spent)
        return self._decide(ledger, budget, amount, status=Status.BLOCK,
                            reason=BlockReason.BUDGET_EXCEEDED,
                            message=f"Budget exceeded: {total_spent} + {amount} > {budget.max_spend}",
                            spent_in_window=total_spent, remaining=remaining)

    def reserve(self, ledger: Ledger, estimate: Decimal, budget: Budget | None = None) -> tuple[str | None, Decision]:
        now = self._clock()
        budget = budget or self.budget_for(ledger)
        try:
            res_id, total_spent = self._store.reserve(ledger, now, estimate, budget)
        except Exception as e:
            return None, self._handle_store_error(ledger, budget, estimate, e)
        if res_id is not None:
            remaining = max(Decimal("0"), budget.max_spend - total_spent)
            return res_id, self._decide(ledger, budget, estimate, status=Status.ALLOW,
                                        spent_in_window=total_spent, remaining=remaining)
        remaining = max(Decimal("0"), budget.max_spend - total_spent)
        return None, self._decide(ledger, budget, estimate, status=Status.BLOCK,
                                  reason=BlockReason.BUDGET_EXCEEDED,
                                  message=f"Budget exceeded: {total_spent} + {estimate} > {budget.max_spend}",
                                  spent_in_window=total_spent, remaining=remaining)

    def commit(self, reservation_id: str, actual: Decimal) -> None:
        self._store.commit(reservation_id, actual)

    def release(self, reservation_id: str) -> None:
        self._store.release(reservation_id)

    def enforce(self, decision: Decision) -> None:
        if decision.blocked and decision.budget.mode == Mode.HARD:
            raise BudgetExceeded(decision)

    def get_remaining(self, ledger: Ledger, budget: Budget | None = None) -> Decimal:
        now = self._clock()
        budget = budget or self.budget_for(ledger)
        spent = self._store.get_spend(ledger, now, budget.window)
        return max(Decimal("0"), budget.max_spend - spent)

    def clear(self, ledger: Ledger) -> None:
        self._store.clear(ledger)

    def clear_all(self) -> None:
        self._store.clear_all()

    # ─────────────────────────────────────────────────────────────
    # Decorator API
    # ─────────────────────────────────────────────────────────────

    def guard(self, ledger: Ledger, budget: Budget | None = None, *, cost: Decimal) -> Callable[[Callable[P, T]], Callable[P, T]]:
        if budget is not None:
            self.register(ledger, budget)
        def decorator(fn: Callable[P, T]) -> Callable[P, T]:
            @wraps(fn)
            def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
                decision = self.check(ledger, cost)
                if decision.blocked:
                    raise BudgetExceeded(decision)
                return fn(*args, **kwargs)
            return wrapper
        return decorator

    def guard_bounded(self, ledger: Ledger, budget: Budget | None = None, *, estimate: Decimal, actual: Callable[[T], Decimal]) -> Callable[[Callable[P, T]], Callable[P, T]]:
        if budget is not None:
            self.register(ledger, budget)
        def decorator(fn: Callable[P, T]) -> Callable[P, T]:
            @wraps(fn)
            def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
                res_id, decision = self.reserve(ledger, estimate)
                if decision.blocked:
                    raise BudgetExceeded(decision)
                try:
                    result = fn(*args, **kwargs)
                    actual_cost = actual(result)
                    self.commit(res_id, actual_cost)  # type: ignore[arg-type]
                    return result
                except BudgetExceeded:
                    raise
                except Exception:
                    if res_id is not None:
                        self.release(res_id)
                    raise
            return wrapper
        return decorator

    def guard_result(self, ledger: Ledger, budget: Budget | None = None, *, cost: Decimal) -> Callable[[Callable[P, T]], Callable[P, Result[T]]]:
        if budget is not None:
            self.register(ledger, budget)
        def decorator(fn: Callable[P, T]) -> Callable[P, Result[T]]:
            @wraps(fn)
            def wrapper(*args: P.args, **kwargs: P.kwargs) -> Result[T]:
                decision = self.check(ledger, cost)
                if decision.blocked:
                    return Result(decision=decision)
                value = fn(*args, **kwargs)
                return Result(decision=decision, _value=value)
            return wrapper
        return decorator

    def guard_bounded_result(self, ledger: Ledger, budget: Budget | None = None, *, estimate: Decimal, actual: Callable[[T], Decimal]) -> Callable[[Callable[P, T]], Callable[P, Result[T]]]:
        if budget is not None:
            self.register(ledger, budget)
        def decorator(fn: Callable[P, T]) -> Callable[P, Result[T]]:
            @wraps(fn)
            def wrapper(*args: P.args, **kwargs: P.kwargs) -> Result[T]:
                res_id, decision = self.reserve(ledger, estimate)
                if decision.blocked:
                    return Result(decision=decision)
                try:
                    result = fn(*args, **kwargs)
                    actual_cost = actual(result)
                    self.commit(res_id, actual_cost)  # type: ignore[arg-type]
                    return Result(decision=decision, _value=result)
                except Exception:
                    if res_id is not None:
                        self.release(res_id)
                    raise
            return wrapper
        return decorator

    # ─────────────────────────────────────────────────────────────
    # Internal
    # ─────────────────────────────────────────────────────────────

    def _handle_store_error(self, ledger: Ledger, budget: Budget, amount: Decimal, error: Exception) -> Decision:
        if budget.on_store_error == StoreErrorMode.FAIL_OPEN:
            return self._decide(ledger, budget, amount, status=Status.ALLOW,
                                reason=BlockReason.STORE_ERROR, message=f"Store error (fail-open): {error}")
        else:
            return self._decide(ledger, budget, amount, status=Status.BLOCK,
                                reason=BlockReason.STORE_ERROR, message=f"Store error (fail-closed): {error}")

    def _decide(self, ledger: Ledger, budget: Budget, amount: Decimal, *, status: Status,
                reason: BlockReason | None = None, message: str | None = None,
                spent_in_window: Decimal = Decimal("0"), remaining: Decimal = Decimal("0")) -> Decision:
        decision = Decision(status=status, ledger=ledger, budget=budget, reason=reason,
                            message=message, spent_in_window=spent_in_window,
                            requested=amount, remaining=remaining)
        self._emitter.emit(decision)
        return decision
