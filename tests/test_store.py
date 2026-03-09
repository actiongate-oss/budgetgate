"""BudgetGate store tests — runs against MemoryStore, optionally against RedisStore.

Usage:
    python3 tests/test_store.py              # MemoryStore only
    REDIS_URL=redis://localhost python3 tests/test_store.py  # Both stores
"""

from __future__ import annotations

import os
import sys
import traceback
from decimal import Decimal

sys.path.insert(0, ".")
from budgetgate import Budget, Ledger
from budgetgate.store import MemoryStore, SpendEvent

passed = 0
failed = 0
errors: list[str] = []


def test(name):
    def decorator(fn):
        global passed, failed
        try:
            fn()
            passed += 1
            print(f"  PASS  {name}")
        except Exception as e:
            failed += 1
            errors.append(f"  FAIL  {name}: {e}\n{traceback.format_exc()}")
            print(f"  FAIL  {name}: {e}")
        return fn
    return decorator


def run_store_tests(make_store, label):
    """Run full protocol tests against any Store implementation."""
    print(f"\n── {label} ──")

    ledger = Ledger("openai", "gpt-4", "user:1")
    budget = Budget(max_spend=Decimal("10.00"), window=60.0)

    @test(f"{label}: check_and_reserve allow")
    def _():
        s = make_store()
        total, ok = s.check_and_reserve(ledger, 1.0, Decimal("3.00"), budget)
        assert ok, f"expected allow, got block (total={total})"
        assert total == Decimal("3.00"), f"expected 3.00, got {total}"

    @test(f"{label}: check_and_reserve block")
    def _():
        s = make_store()
        s.check_and_reserve(ledger, 1.0, Decimal("8.00"), budget)
        total, ok = s.check_and_reserve(ledger, 2.0, Decimal("3.00"), budget)
        assert not ok, "expected block"
        assert total == Decimal("8.00"), f"expected 8.00, got {total}"

    @test(f"{label}: check_and_reserve window pruning")
    def _():
        s = make_store()
        s.check_and_reserve(ledger, 1.0, Decimal("9.00"), budget)
        # 62 seconds later, outside 60s window
        total, ok = s.check_and_reserve(ledger, 63.0, Decimal("5.00"), budget)
        assert ok, f"expected allow after window prune, got block (total={total})"

    @test(f"{label}: reserve and commit")
    def _():
        s = make_store()
        res_id, total = s.reserve(ledger, 1.0, Decimal("5.00"), budget)
        assert res_id is not None, "expected reservation id"
        assert total == Decimal("5.00"), f"expected 5.00, got {total}"
        # Reservation holds spend — new check should see it
        total2, ok = s.check_and_reserve(ledger, 2.0, Decimal("6.00"), budget)
        assert not ok, "expected block (5.00 reserved + 6.00 > 10.00)"
        # Commit with lower actual
        s.commit(res_id, Decimal("2.00"))
        # Now: 2.00 committed, reservation released
        total3 = s.get_spend(ledger, 3.0, 60.0)
        assert total3 == Decimal("2.00"), f"expected 2.00 after commit, got {total3}"

    @test(f"{label}: reserve and release")
    def _():
        s = make_store()
        res_id, _ = s.reserve(ledger, 1.0, Decimal("5.00"), budget)
        assert res_id is not None
        s.release(res_id)
        # Reservation released — full budget available
        total = s.get_spend(ledger, 2.0, 60.0)
        assert total == Decimal("0"), f"expected 0 after release, got {total}"

    @test(f"{label}: reserve block when over budget")
    def _():
        s = make_store()
        s.check_and_reserve(ledger, 1.0, Decimal("8.00"), budget)
        res_id, total = s.reserve(ledger, 2.0, Decimal("3.00"), budget)
        assert res_id is None, "expected block"

    @test(f"{label}: commit unknown raises KeyError")
    def _():
        s = make_store()
        try:
            s.commit("nonexistent", Decimal("1.00"))
            assert False, "expected KeyError"
        except KeyError:
            pass

    @test(f"{label}: release unknown raises KeyError")
    def _():
        s = make_store()
        try:
            s.release("nonexistent")
            assert False, "expected KeyError"
        except KeyError:
            pass

    @test(f"{label}: get_spend includes reservations")
    def _():
        s = make_store()
        s.check_and_reserve(ledger, 1.0, Decimal("3.00"), budget)
        res_id, _ = s.reserve(ledger, 2.0, Decimal("2.00"), budget)
        total = s.get_spend(ledger, 3.0, 60.0)
        assert total == Decimal("5.00"), f"expected 5.00, got {total}"

    @test(f"{label}: clear removes spends and reservations")
    def _():
        s = make_store()
        s.check_and_reserve(ledger, 1.0, Decimal("5.00"), budget)
        s.reserve(ledger, 2.0, Decimal("3.00"), budget)
        s.clear(ledger)
        total = s.get_spend(ledger, 3.0, 60.0)
        assert total == Decimal("0"), f"expected 0 after clear, got {total}"

    @test(f"{label}: clear_all")
    def _():
        s = make_store()
        l2 = Ledger("anthropic", "claude", "user:2")
        s.check_and_reserve(ledger, 1.0, Decimal("5.00"), budget)
        s.check_and_reserve(l2, 1.0, Decimal("3.00"), budget)
        s.clear_all()
        assert s.get_spend(ledger, 2.0, 60.0) == Decimal("0")
        assert s.get_spend(l2, 2.0, 60.0) == Decimal("0")

    @test(f"{label}: unbounded window (None)")
    def _():
        unbounded = Budget(max_spend=Decimal("100.00"), window=None)
        s = make_store()
        s.check_and_reserve(ledger, 1.0, Decimal("50.00"), unbounded)
        # Far in the future — still counts
        total, ok = s.check_and_reserve(ledger, 999999.0, Decimal("51.00"), unbounded)
        assert not ok, "expected block (unbounded window, 50 + 51 > 100)"


# ── Run tests ──

run_store_tests(MemoryStore, "MemoryStore")

# Optional Redis tests
redis_url = os.environ.get("REDIS_URL")
if redis_url:
    try:
        import redis
        from budgetgate.store import RedisStore

        client = redis.Redis.from_url(redis_url, decode_responses=True)
        client.ping()

        def make_redis():
            # Use unique prefix per test run to avoid collisions
            import secrets
            prefix = f"bgtest:{secrets.token_hex(4)}"
            store = RedisStore(client, prefix=prefix)
            return store

        run_store_tests(make_redis, "RedisStore")
    except ImportError:
        print("\n  SKIP  RedisStore: redis-py not installed")
    except redis.ConnectionError:
        print(f"\n  SKIP  RedisStore: cannot connect to {redis_url}")
else:
    print("\n  SKIP  RedisStore: set REDIS_URL to enable")

print(f"\n{'═' * 50}")
print(f"Results: {passed} passed, {failed} failed")
if errors:
    print("\nFailures:")
    for e in errors:
        print(e)
print(f"{'═' * 50}")
sys.exit(1 if failed else 0)
