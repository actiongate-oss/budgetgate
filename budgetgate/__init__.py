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
from .engine import BudgetExceeded, Engine
from .store import MemoryStore, Reservation, SpendEvent, Store

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
    "BudgetExceeded",
    # Store
    "Store",
    "MemoryStore",
    "SpendEvent",
    "Reservation",
]

__version__ = "0.2.0"
