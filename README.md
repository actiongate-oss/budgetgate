# BudgetGate

Deterministic, pre-execution spend limiting for semantic actions in agent systems.

## Source of Truth

The canonical source is [github.com/actiongate-oss/budgetgate](https://github.com/actiongate-oss/budgetgate). PyPI distribution is a convenience mirror.

**Vendoring encouraged.** This is a small, stable primitive. Copy it, fork it, reimplement it. See [SEMANTICS.md](SEMANTICS.md) for the behavioral contract if you reimplement.

---

## Quick Start

```python
from decimal import Decimal
from budgetgate import Engine, Ledger, Budget, BudgetExceeded

engine = Engine()

@engine.guard(
    Ledger("openai", "gpt-4", "user:123"),
    Budget(max_spend=Decimal("10.00"), window=3600),  # $10/hour
    cost=Decimal("0.03"),  # fixed cost per call
)
def call_gpt4(prompt: str) -> str:
    return openai.chat(prompt)

try:
    response = call_gpt4("Hello")
except BudgetExceeded as e:
    print(f"Budget exceeded: {e.decision.spent_in_window} spent")
```

---

## Two Cost Modes

### Fixed Cost (pre-execution)

When cost is known before execution:

```python
@engine.guard(
    Ledger("openai", "embedding"),
    Budget(max_spend=Decimal("5.00"), window=3600),
    cost=Decimal("0.0001"),  # fixed cost per call
)
def embed(text: str) -> list[float]:
    return openai.embed(text)
```

### Bounded Dynamic Cost (pre-execution with estimate)

When cost depends on the result but has a known upper bound:

```python
@engine.guard_bounded(
    Ledger("anthropic", "claude", "user:123"),
    Budget(max_spend=Decimal("5.00"), window=3600),
    estimate=Decimal("0.50"),  # max possible cost (reserved before execution)
    actual=lambda r: Decimal(str(r.usage.total_cost)),  # actual cost (committed after)
)
def call_claude(prompt: str) -> Response:
    return anthropic.messages.create(...)
```

The estimate is reserved before execution. If it doesn't fit the budget, the action is blocked. After execution, the actual cost is committed and unused budget is recovered.

---

## Core Concepts

### Ledger

Identifies a spend-tracked stream:

```python
Ledger(namespace, resource, principal)

Ledger("openai", "gpt-4", "user:123")     # per-user
Ledger("anthropic", "claude", "team:eng") # per-team
Ledger("infra", "compute", "global")      # global
```

### Budget

```python
Budget(
    max_spend=Decimal("10.00"),  # max spend in window
    window=3600,                  # rolling window (seconds)
    mode=Mode.HARD,               # HARD raises, SOFT returns result
    on_store_error=StoreErrorMode.FAIL_CLOSED,
)
```

### Decision

Every check returns a Decision with:

```python
decision.allowed          # bool
decision.spent_in_window  # Decimal - current spend
decision.remaining        # Decimal - budget remaining
decision.requested        # Decimal - amount requested
```

---

## Decorator Styles

| Decorator | Cost Mode | Returns | On Block |
|-----------|-----------|---------|----------|
| `guard` | Fixed | `T` | Raises `BudgetExceeded` |
| `guard_bounded` | Dynamic | `T` | Raises `BudgetExceeded` |
| `guard_result` | Fixed | `Result[T]` | Returns blocked result |
| `guard_bounded_result` | Dynamic | `Result[T]` | Returns blocked result |

```python
# Raises on block
@engine.guard(ledger, budget, cost=Decimal("0.01"))
def fixed_action(): ...

@engine.guard_bounded(ledger, budget, estimate=Decimal("0.50"), actual=lambda r: r.cost)
def dynamic_action(): ...

# Never raises - returns Result[T]
@engine.guard_result(ledger, budget, cost=Decimal("0.01"))
def fixed_action(): ...

@engine.guard_bounded_result(ledger, budget, estimate=Decimal("0.50"), actual=lambda r: r.cost)
def dynamic_action(): ...
```

---

## Relation to ActionGate

BudgetGate complements [ActionGate](https://github.com/actiongate-oss/actiongate):

| Primitive | Limits | Use case |
|-----------|--------|----------|
| ActionGate | calls/time | Rate limiting |
| BudgetGate | cost/time | Spend limiting |

Both are:
- Deterministic
- Pre-execution
- Decorator-friendly
- Store-backed

Use together:

```python
from decimal import Decimal

@actiongate_engine.guard(Gate("api", "search"), Policy(max_calls=100))
@budgetgate_engine.guard(Ledger("api", "search"), Budget(max_spend=Decimal("1.00")), cost=Decimal("0.01"))
def search(query: str) -> list:
    ...
```

---

## API Reference

| Type | Purpose |
|------|---------|
| `Engine` | Core spend tracking |
| `Ledger` | Spend stream identity |
| `Budget` | Spend policy |
| `Decision` | Evaluation result |
| `Result[T]` | Wrapper for `guard_result` |
| `BudgetExceeded` | Exception from `guard` |

| Enum | Values |
|------|--------|
| `Mode` | `HARD`, `SOFT` |
| `StoreErrorMode` | `FAIL_CLOSED`, `FAIL_OPEN` |
| `Status` | `ALLOW`, `BLOCK` |
| `BlockReason` | `BUDGET_EXCEEDED`, `STORE_ERROR` |

---

## Numeric Precision

All spend amounts use `Decimal` to avoid floating-point drift. See [SEMANTICS.md](SEMANTICS.md) §9.

---

## License

Apache License 2.0. See [LICENSE](LICENSE) for the full text.
