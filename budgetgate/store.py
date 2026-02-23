"""Storage backends for BudgetGate."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol
from uuid import uuid4
import threading

from .core import Budget, Ledger


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
        """Atomically check budget and reserve spend if allowed."""
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

                # Allowed - record spend immediately (no reservation needed)
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
        """Reserve spend without committing."""
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

                # Create reservation
                res_id = uuid4().hex
                self._reservations[res_id] = Reservation(
                    id=res_id,
                    ledger=ledger,
                    ts=now,
                    amount=amount,
                )
                return res_id, current_spend + amount

    def commit(self, reservation_id: str, actual: Decimal) -> None:
        """Commit a reservation with actual spend."""
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
        """Release a reservation without committing."""
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
        """Get current spend in window (read-only)."""
        with self._global_lock:
            lock = self._get_lock(ledger)
            with lock:
                events = self._ledgers.get(ledger, [])
                pruned = self._prune(events, now, window)
                committed = sum((e.amount for e in pruned), Decimal("0"))
                reserved = self._get_reserved_locked(ledger, now, window)
                return committed + reserved

    def clear(self, ledger: Ledger) -> None:
        """Clear all spend history for a ledger."""
        with self._global_lock:
            lock = self._get_lock(ledger)
            with lock:
                self._ledgers.pop(ledger, None)
                # Also clear reservations for this ledger
                to_remove = [
                    rid for rid, res in self._reservations.items()
                    if res.ledger == ledger
                ]
                for rid in to_remove:
                    del self._reservations[rid]

    def clear_all(self) -> None:
        """Clear all spend history."""
        with self._global_lock:
            self._ledgers.clear()
            self._reservations.clear()
            self._locks.clear()
