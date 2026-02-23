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
from .store import MemoryStore, Store

P = ParamSpec("P")
T = TypeVar("T")


class BudgetExceeded(RuntimeError):
    """Raised when spend would exceed budget in HARD mode."""

    def __init__(self, decision: Decision) -> None:
        super().__init__(decision.message or f"Budget exceeded: {decision.reason}")
        self.decision = decision


class Engine:
    """BudgetGate engine for spend limiting agent actions.
    
    BudgetGate enforces economic constraints before action execution.
    It supports two modes:
    
    1. Fixed cost (truly pre-execution):
       Cost is known before execution. Atomic check-and-reserve.
       
    2. Bounded dynamic cost (pre-execution with estimate):
       Cost is known only after execution, but bounded by estimate.
       Reserves estimate, commits actual, releases difference.
    
    Example:
        engine = Engine()
        
        # Fixed cost - known before execution
        @engine.guard(
            Ledger("openai", "embedding"),
            Budget(max_spend=Decimal("10.00"), window=3600),
            cost=Decimal("0.0001"),
        )
        def embed(text: str) -> list[float]:
            return openai.embed(text)
        
        # Bounded dynamic cost - estimate before, actual after
        @engine.guard_bounded(
            Ledger("openai", "gpt-4"),
            Budget(max_spend=Decimal("50.00"), window=3600),
            estimate=Decimal("0.50"),  # Max possible cost
            actual=lambda r: Decimal(str(r.usage.total_cost)),
        )
        def chat(prompt: str) -> Response:
            return openai.chat(prompt)
    """

    __slots__ = ("_store", "_clock", "_budgets", "_listeners", "_errors")

    def __init__(
        self,
        store: Store | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._store: Store = store or MemoryStore()
        self._clock = clock or time.monotonic
        self._budgets: dict[Ledger, Budget] = {}
        self._listeners: list[Callable[[Decision], None]] = []
        self._errors = 0

    # ─────────────────────────────────────────────────────────────
    # Configuration
    # ─────────────────────────────────────────────────────────────

    def register(self, ledger: Ledger, budget: Budget) -> None:
        """Register a budget for a ledger."""
        self._budgets[ledger] = budget

    def budget_for(self, ledger: Ledger) -> Budget:
        """Get budget for ledger (default infinite if not registered)."""
        return self._budgets.get(
            ledger,
            Budget(max_spend=Decimal("Infinity")),
        )

    def on_decision(self, listener: Callable[[Decision], None]) -> None:
        """Add a listener for decisions (for logging/metrics)."""
        self._listeners.append(listener)

    @property
    def listener_errors(self) -> int:
        """Count of listener exceptions (never block execution)."""
        return self._errors

    # ─────────────────────────────────────────────────────────────
    # Core API
    # ─────────────────────────────────────────────────────────────

    def check(
        self,
        ledger: Ledger,
        amount: Decimal,
        budget: Budget | None = None,
    ) -> Decision:
        """Check if spend is allowed and reserve if so.
        
        This is atomic: if ALLOW is returned, the spend is already reserved.
        """
        now = self._clock()
        budget = budget or self.budget_for(ledger)

        try:
            total_spent, allowed = self._store.check_and_reserve(
                ledger, now, amount, budget
            )
        except Exception as e:
            return self._handle_store_error(ledger, budget, amount, e)

        if allowed:
            remaining = max(Decimal("0"), budget.max_spend - total_spent)
            return self._decide(
                ledger,
                budget,
                amount,
                status=Status.ALLOW,
                spent_in_window=total_spent,
                remaining=remaining,
            )

        remaining = max(Decimal("0"), budget.max_spend - total_spent)
        return self._decide(
            ledger,
            budget,
            amount,
            status=Status.BLOCK,
            reason=BlockReason.BUDGET_EXCEEDED,
            message=f"Budget exceeded: {total_spent} + {amount} > {budget.max_spend}",
            spent_in_window=total_spent,
            remaining=remaining,
        )

    def reserve(
        self,
        ledger: Ledger,
        estimate: Decimal,
        budget: Budget | None = None,
    ) -> tuple[str | None, Decision]:
        """Reserve spend without committing.
        
        Used for bounded dynamic costs.
        
        Returns:
            (reservation_id, decision)
            reservation_id is None if blocked.
        """
        now = self._clock()
        budget = budget or self.budget_for(ledger)

        try:
            res_id, total_spent = self._store.reserve(ledger, now, estimate, budget)
        except Exception as e:
            return None, self._handle_store_error(ledger, budget, estimate, e)

        if res_id is not None:
            remaining = max(Decimal("0"), budget.max_spend - total_spent)
            decision = self._decide(
                ledger,
                budget,
                estimate,
                status=Status.ALLOW,
                spent_in_window=total_spent,
                remaining=remaining,
            )
            return res_id, decision

        remaining = max(Decimal("0"), budget.max_spend - total_spent)
        decision = self._decide(
            ledger,
            budget,
            estimate,
            status=Status.BLOCK,
            reason=BlockReason.BUDGET_EXCEEDED,
            message=f"Budget exceeded: {total_spent} + {estimate} > {budget.max_spend}",
            spent_in_window=total_spent,
            remaining=remaining,
        )
        return None, decision

    def commit(self, reservation_id: str, actual: Decimal) -> None:
        """Commit a reservation with actual spend."""
        self._store.commit(reservation_id, actual)

    def release(self, reservation_id: str) -> None:
        """Release a reservation without committing (e.g., on failure)."""
        self._store.release(reservation_id)

    def enforce(self, decision: Decision) -> None:
        """Raise BudgetExceeded if decision is blocked in HARD mode."""
        if decision.blocked and decision.budget.mode == Mode.HARD:
            raise BudgetExceeded(decision)

    def get_remaining(self, ledger: Ledger, budget: Budget | None = None) -> Decimal:
        """Get remaining budget (read-only, no reservation)."""
        now = self._clock()
        budget = budget or self.budget_for(ledger)
        spent = self._store.get_spend(ledger, now, budget.window)
        return max(Decimal("0"), budget.max_spend - spent)

    def clear(self, ledger: Ledger) -> None:
        """Clear spend history for a ledger."""
        self._store.clear(ledger)

    def clear_all(self) -> None:
        """Clear all spend history."""
        self._store.clear_all()

    # ─────────────────────────────────────────────────────────────
    # Decorator API
    # ─────────────────────────────────────────────────────────────

    def guard(
        self,
        ledger: Ledger,
        budget: Budget | None = None,
        *,
        cost: Decimal,
    ) -> Callable[[Callable[P, T]], Callable[P, T]]:
        """Decorator for fixed-cost actions (truly pre-execution).
        
        Cost must be known before execution. Atomic check-and-reserve.
        
        - HARD mode (default): raises BudgetExceeded on block
        - SOFT mode: raises BudgetExceeded on block (use guard_result for no-raise)
        
        Example:
            @engine.guard(
                Ledger("openai", "embedding"),
                Budget(max_spend=Decimal("10.00")),
                cost=Decimal("0.0001"),
            )
            def embed(text: str) -> list[float]:
                return openai.embed(text)
        """
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

    def guard_bounded(
        self,
        ledger: Ledger,
        budget: Budget | None = None,
        *,
        estimate: Decimal,
        actual: Callable[[T], Decimal],
    ) -> Callable[[Callable[P, T]], Callable[P, T]]:
        """Decorator for bounded dynamic-cost actions.
        
        Reserves `estimate` before execution, commits `actual(result)` after.
        This is still pre-execution gating: if estimate doesn't fit, action is blocked.
        
        - HARD mode (default): raises BudgetExceeded on block
        - SOFT mode: raises BudgetExceeded on block (use guard_bounded_result for no-raise)
        
        Example:
            @engine.guard_bounded(
                Ledger("openai", "gpt-4"),
                Budget(max_spend=Decimal("50.00")),
                estimate=Decimal("0.50"),
                actual=lambda r: Decimal(str(r.usage.total_cost)),
            )
            def chat(prompt: str) -> Response:
                return openai.chat(prompt)
        """
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

    def guard_result(
        self,
        ledger: Ledger,
        budget: Budget | None = None,
        *,
        cost: Decimal,
    ) -> Callable[[Callable[P, T]], Callable[P, Result[T]]]:
        """Decorator for fixed-cost actions that returns Result[T] (never raises).
        
        Example:
            @engine.guard_result(
                Ledger("openai", "embedding"),
                Budget(max_spend=Decimal("10.00"), mode=Mode.SOFT),
                cost=Decimal("0.0001"),
            )
            def embed(text: str) -> list[float]:
                return openai.embed(text)
            
            result = embed("hello")
            if result.ok:
                vectors = result.unwrap()
        """
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

    def guard_bounded_result(
        self,
        ledger: Ledger,
        budget: Budget | None = None,
        *,
        estimate: Decimal,
        actual: Callable[[T], Decimal],
    ) -> Callable[[Callable[P, T]], Callable[P, Result[T]]]:
        """Decorator for bounded dynamic-cost actions that returns Result[T] (never raises).
        
        Example:
            @engine.guard_bounded_result(
                Ledger("openai", "gpt-4"),
                Budget(max_spend=Decimal("50.00"), mode=Mode.SOFT),
                estimate=Decimal("0.50"),
                actual=lambda r: Decimal(str(r.usage.total_cost)),
            )
            def chat(prompt: str) -> Response:
                return openai.chat(prompt)
            
            result = chat("hello")
            response = result.unwrap_or(fallback_response)
        """
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

    def _handle_store_error(
        self,
        ledger: Ledger,
        budget: Budget,
        amount: Decimal,
        error: Exception,
    ) -> Decision:
        """Handle store errors according to policy."""
        if budget.on_store_error == StoreErrorMode.FAIL_OPEN:
            return self._decide(
                ledger,
                budget,
                amount,
                status=Status.ALLOW,
                reason=BlockReason.STORE_ERROR,
                message=f"Store error (fail-open): {error}",
            )
        else:
            return self._decide(
                ledger,
                budget,
                amount,
                status=Status.BLOCK,
                reason=BlockReason.STORE_ERROR,
                message=f"Store error (fail-closed): {error}",
            )

    def _decide(
        self,
        ledger: Ledger,
        budget: Budget,
        amount: Decimal,
        *,
        status: Status,
        reason: BlockReason | None = None,
        message: str | None = None,
        spent_in_window: Decimal = Decimal("0"),
        remaining: Decimal = Decimal("0"),
    ) -> Decision:
        decision = Decision(
            status=status,
            ledger=ledger,
            budget=budget,
            reason=reason,
            message=message,
            spent_in_window=spent_in_window,
            requested=amount,
            remaining=remaining,
        )
        self._emit(decision)
        return decision

    def _emit(self, decision: Decision) -> None:
        for listener in self._listeners:
            try:
                listener(decision)
            except Exception:
                self._errors += 1
