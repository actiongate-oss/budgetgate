"""BudgetGate: Deterministic spend governance for agent systems.

Does not decide what to run—decides whether an action may execute at all,
under a deterministic economic contract. Pairs with ActionGate.
"""

from .core import (
    MISSING,
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
from .engine import BudgetExceededError, Engine
from .store import (
    AsyncMemoryStore,
    AsyncStore,
    MemoryStore,
    RedisStore,
    Reservation,
    SpendEvent,
    Store,
)

__all__ = [
    # Core types
    "Ledger",
    "Budget",
    "Decision",
    "Result",
    "MISSING",
    # Enums
    "Mode",
    "Status",
    "BlockReason",
    "StoreErrorMode",
    # Engine
    "Engine",
    "BudgetExceededError",
    "Emitter",
    # Store
    "Store",
    "AsyncStore",
    "MemoryStore",
    "AsyncMemoryStore",
    "RedisStore",
    "SpendEvent",
    "Reservation",
]

__version__ = "0.3.1"
