# BudgetGate Semantics

This document defines the normative behavior of BudgetGate. Any implementation claiming compatibility must conform to these semantics.

Version: 0.1

---

## 1. Purpose

BudgetGate is a **deterministic, pre-execution spend gate** for AI agents. It does not decide what to run—it decides whether an action may execute at all, under a deterministic economic contract.

It is not a billing system, cost estimator, usage analytics platform, or financial ledger.

---

## 2. Spend Identity

A spend stream is identified by a **Ledger**, a 3-tuple:

```
Ledger = (namespace: string, resource: string, principal: string)
```

| Field       | Purpose              | Examples                                |
|-------------|----------------------|-----------------------------------------|
| `namespace` | Provider or domain   | `"openai"`, `"anthropic"`, `"infra"`    |
| `resource`  | Billable resource    | `"gpt-4"`, `"claude"`, `"compute"`      |
| `principal` | Scope of enforcement | `"user:123"`, `"team:eng"`, `"global"`  |

Two ledgers are equal if and only if all three fields are equal. Spend state is **not shared** across distinct ledgers.

---

## 3. Budget

A **Budget** defines spend policy:

| Parameter        | Type                    | Meaning                                     |
|------------------|-------------------------|---------------------------------------------|
| `max_spend`      | Decimal ≥ 0             | Maximum allowed spend within `window`       |
| `window`         | float > 0 \| null       | Rolling window in seconds; null = unbounded |
| `mode`           | HARD \| SOFT            | HARD raises on block; SOFT returns decision |
| `on_store_error` | FAIL_CLOSED \| FAIL_OPEN| Behavior when storage backend fails         |

Spend amounts **must** use `Decimal` to avoid floating-point drift in financial calculations.

---

## 4. Enforcement Modes

BudgetGate supports two enforcement modes based on when cost is known:

### 4.1 Fixed Cost (Truly Pre-Execution)

When cost is known before execution:

1. Atomically check if `spent + cost ≤ max_spend`
2. If yes: reserve spend, return ALLOW, execute action
3. If no: return BLOCK, action does not execute

This is fully deterministic and race-free.

### 4.2 Bounded Dynamic Cost (Pre-Execution with Estimate)

When cost is known only after execution but has a known upper bound:

1. **Reserve**: Atomically check if `spent + estimate ≤ max_spend`
   - If yes: create reservation for `estimate`, return ALLOW
   - If no: return BLOCK, action does not execute
2. **Execute**: Run the action
3. **Commit**: Replace reservation with `actual` cost (where `actual ≤ estimate`)
4. **On failure**: Release reservation (no spend recorded)

This is pre-execution gating: if the estimate doesn't fit the budget, the action is blocked before execution.

### 4.3 Unbounded Dynamic Cost (NOT SUPPORTED)

BudgetGate does **not** support dynamic costs without an estimate. If you cannot bound the cost, you cannot use BudgetGate for pre-execution enforcement.

Rationale: Without an upper bound, concurrent calls can race past the budget check and overspend. This violates the determinism guarantee.

---

## 5. Decision Logic

Given a ledger L and budget B, at time T, for amount A:

1. **Prune**: Remove all recorded spends older than `T - B.window` (if window is set)
2. **Sum**: Let `spent` = sum of committed spends + sum of active reservations in window
3. **Check**: If `spent + A > B.max_spend` → **BLOCK** (reason: BUDGET_EXCEEDED)
4. Otherwise → **ALLOW**

### 5.1 Reservation Accounting

Active reservations count toward `spent` until committed or released. This prevents concurrent calls from both reserving the same budget headroom.

### 5.2 Commit Adjusts, Does Not Add

When committing a reservation:
- The reservation amount is removed
- The actual amount is added
- Net effect: `spent` changes by `(actual - estimate)`

If `actual < estimate`, budget headroom is recovered.

---

## 6. Atomicity

The check-and-reserve operation **must be atomic** with respect to concurrent callers on the same ledger.

For fixed costs: single atomic operation.
For bounded costs: reserve is atomic; commit/release must be safe to call exactly once and fail cleanly (e.g., raise or return error) if the reservation does not exist.

Implementations using shared storage (Redis, database) must use atomic primitives (e.g., Lua scripts, transactions).

A non-atomic implementation may allow overspend under concurrency. This is a conformance violation.

---

## 7. Failure Semantics

When the storage backend is unavailable or errors:

| `on_store_error` | Behavior                             |
|------------------|--------------------------------------|
| `FAIL_CLOSED`    | Return BLOCK with reason STORE_ERROR |
| `FAIL_OPEN`      | Return ALLOW with reason STORE_ERROR |

The decision **must** include the `STORE_ERROR` reason to distinguish from budget blocks.

### 7.1 Reservation Failure

If execution fails after reservation:
- Implementation **must** release the reservation
- No spend is recorded
- Budget headroom is recovered

---

## 8. Decision Structure

Every evaluation **must** return a Decision containing at minimum:

| Field             | Type               | Meaning                                  |
|-------------------|--------------------|------------------------------------------|
| `status`          | ALLOW \| BLOCK     | Outcome                                  |
| `ledger`          | Ledger             | The evaluated ledger                     |
| `budget`          | Budget             | The budget policy used                   |
| `reason`          | BlockReason \| null| Why blocked (null if allowed)            |
| `spent_in_window` | Decimal            | Total spend at decision time             |
| `requested`       | Decimal            | Amount requested                         |
| `remaining`       | Decimal            | Budget remaining (clamped to ≥ 0)        |

This enables full observability and auditability of every decision.

---

## 9. Numeric Precision

All spend amounts **must** use `Decimal` (or equivalent arbitrary-precision type).

Rationale: Floating-point arithmetic causes drift (e.g., `0.1 + 0.2 ≠ 0.3`). For financial calculations, even small errors accumulate and cause incorrect blocking decisions.

Implementations **may** internally use integer micros (e.g., microdollars) if precision is preserved.

---

## 10. Out of Scope

BudgetGate **does not** and **must not**:

- Make LLM or model inference calls
- Estimate costs from request parameters
- Track billing or generate invoices
- Provide authentication or authorization
- Implement alerts, notifications, or dashboards
- Make decisions based on action content or arguments
- Support unbounded dynamic costs

Provider-enforced quotas and billing caps (e.g., OpenAI spend limits, AWS budgets) are external failures, not BudgetGate enforcement. BudgetGate operates on local state; it cannot observe or enforce provider-side constraints.

BudgetGate is a **stateless-per-request, stateful-per-ledger** primitive. It examines only the ledger identity and spend amount, never the payload.

### 10.1 Reservation Lifecycle (Non-Normative)

Implementations may apply TTLs to orphaned reservations to recover headroom after crashes or network partitions. This is not required for conformance but is recommended for production deployments.

---

## 11. Compatibility

An implementation is **BudgetGate-compatible** if and only if:

1. It implements the Ledger identity model (§2)
2. It implements the Budget parameters (§3)
3. It supports both fixed and bounded dynamic costs (§4)
4. It follows the decision logic exactly (§5)
5. Check-and-reserve is atomic (§6)
6. Failure modes match the specification (§7)
7. Decisions include all required fields (§8)
8. Numeric precision is maintained (§9)
9. It does not extend scope beyond §10

Compatible implementations may:

- Use any storage backend
- Be written in any language
- Add non-normative fields to Decision
- Provide additional observability hooks

Compatible implementations must not:

- Change the decision logic
- Support unbounded dynamic costs with pre-execution claims
- Use floating-point for spend amounts
- Skip reservation accounting for concurrent safety

---

## 12. Reference Implementation

The canonical reference implementation is at:

```
https://github.com/actiongate-oss/budgetgate
```

When this specification and the reference implementation conflict, **this specification governs**.

---

## Changelog

- **0.1** (2026-01): Initial specification
