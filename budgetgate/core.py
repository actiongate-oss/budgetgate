"""Core types for BudgetGate."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum, auto
from typing import Optional


class Mode(Enum):
    """Enforcement mode for blocked spends."""
    HARD = auto()  # Raise exception on block
    SOFT = auto()  # Return blocked decision (caller handles)


class StoreErrorMode(Enum):
    """Behavior when store backend fails."""
    FAIL_CLOSED = auto()  # Block spend (safe default)
    FAIL_OPEN = auto()    # Allow spend (availability over safety)


class Status(Enum):
    """Decision outcome."""
    ALLOW = auto()
    BLOCK = auto()


class BlockReason(Enum):
    """Why a spend was blocked."""
    BUDGET_EXCEEDED = auto()  # Would exceed max_spend
    STORE_ERROR = auto()      # Backend failure


@dataclass(frozen=True, slots=True)
class Ledger:
    """Identifies a spend-tracked stream.
    
    Examples:
        Ledger("openai", "gpt-4", "user:123")      # per-user API spend
        Ledger("anthropic", "claude", "team:eng")  # per-team spend
        Ledger("infra", "compute", "global")       # global compute budget
    """
    namespace: str
    resource: str
    principal: str = "global"

    def __str__(self) -> str:
        return f"{self.namespace}:{self.resource}@{self.principal}"

    @property
    def key(self) -> str:
        """Redis-friendly key string."""
        return f"bg:{self.namespace}:{self.resource}:{self.principal}"


@dataclass(frozen=True, slots=True)
class Budget:
    """Spend policy.
    
    Args:
        max_spend: Maximum allowed spend in window (Decimal for precision)
        window: Rolling window in seconds (None = unbounded lifetime budget)
        mode: HARD raises on block, SOFT returns decision
        on_store_error: FAIL_CLOSED blocks, FAIL_OPEN allows
    """
    max_spend: Decimal
    window: float | None = 3600.0  # 1 hour default
    mode: Mode = Mode.HARD
    on_store_error: StoreErrorMode = StoreErrorMode.FAIL_CLOSED

    def __post_init__(self) -> None:
        if self.max_spend < 0:
            raise ValueError("max_spend must be >= 0")
        if self.window is not None and self.window <= 0:
            raise ValueError("window must be > 0 or None")


@dataclass(frozen=True, slots=True)
class Decision:
    """Result of evaluating a spend against its budget."""
    status: Status
    ledger: Ledger
    budget: Budget
    reason: BlockReason | None = None
    message: str | None = None
    spent_in_window: Decimal = Decimal("0")
    requested: Decimal = Decimal("0")
    remaining: Decimal = Decimal("0")

    @property
    def allowed(self) -> bool:
        return self.status == Status.ALLOW

    @property
    def blocked(self) -> bool:
        return self.status == Status.BLOCK

    def __bool__(self) -> bool:
        """Truthy = allowed."""
        return self.allowed

    def to_dict(self) -> dict:
        """Serialize for audit composition."""
        return {
            "status": self.status.name,
            "ledger": str(self.ledger),
            "reason": self.reason.name if self.reason else None,
            "message": self.message,
            "spent_in_window": str(self.spent_in_window),
            "requested": str(self.requested),
            "remaining": str(self.remaining),
        }


class _Missing:
    """Sentinel for distinguishing None from missing value."""
    __slots__ = ()
    def __repr__(self) -> str:
        return "<MISSING>"

MISSING = _Missing()


@dataclass(frozen=True, slots=True)
class Result[T]:
    """Wrapper for guarded function results.
    
    Uses a sentinel to distinguish between:
    - Function returned None (legitimate value)
    - Function was blocked (no value)
    """
    decision: Decision
    _value: T | _Missing = MISSING

    @property
    def ok(self) -> bool:
        return self.decision.allowed

    @property
    def has_value(self) -> bool:
        return not isinstance(self._value, _Missing)

    @property
    def value(self) -> T | None:
        """Get value or None if blocked/missing."""
        if isinstance(self._value, _Missing):
            return None
        return self._value

    def unwrap(self) -> T:
        """Get value or raise if blocked."""
        if isinstance(self._value, _Missing):
            raise ValueError(f"No value: {self.decision.message or 'blocked'}")
        return self._value

    def unwrap_or(self, default: T) -> T:
        """Get value or return default if blocked."""
        if isinstance(self._value, _Missing):
            return default
        return self._value
