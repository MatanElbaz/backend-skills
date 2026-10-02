<!-- result: baseline=3 with=3 runs=3 -->
# Demo: transaction-boundaries.hard

Signal (regex, case-insensitive): `self-invocation|self invocation|bypass(es)? the proxy|rollbackFor|checked exception`

| Arm | Runs mentioning the signal |
|---|---|
| Without the plugin | 3 / 3 |
| With the plugin | 3 / 3 |

## Code reviewed

```java
@Service
public class RefundService {

    @Transactional
    public void refundAll(List<Long> orderIds) {
        for (Long id : orderIds) {
            refundOne(id);
        }
    }

    @Transactional(propagation = Propagation.REQUIRES_NEW)
    public void refundOne(Long id) throws PaymentException {
        Order o = orders.findById(id).orElseThrow();
        o.setStatus(OrderStatus.REFUNDED);
        gateway.refund(o.getPaymentId());   // throws checked PaymentException
    }
}
```

<details><summary>Last output without the plugin</summary>

## 1. `REQUIRES_NEW` is silently ignored — self-invocation bypasses the proxy

`refundAll` calls `refundOne(id)` directly on `this`. Spring's transactional advice lives on the proxy, so the annotation has no effect: all N refunds run inside `refundAll`'s single transaction. The per-order isolation you're paying for with `REQUIRES_NEW` doesn't exist, and one failure rolls back every status update made so far.

Fix: move `refundOne` to a separate bean, inject a self-reference (`@Lazy RefundService self`), or drop the annotation and use `TransactionTemplate` with `PROPAGATION_REQUIRES_NEW` explicitly.

## 2. A checked exception won't roll back the transaction

Spring's default rollback rule covers `RuntimeException` and `Error` only. `PaymentException` is checked, so when the gateway call fails, the transaction **commits** — the order is persisted as `REFUNDED` while no money moved. This is the worst kind of bug in a refund path: silent state divergence from the payment provider.

```java
@Transactional(propagation = REQUIRES_NEW, rollbackFor = PaymentException.class)
```

Note this interacts with #1: today the checked exception propagates out of `refundAll`, whose transaction also won't roll back for it.

## 3. As written, this doesn't compile

`refundOne` declares `throws PaymentException`; `refundAll` neither catches nor declares it. Either the real code differs from what's pasted, or `PaymentException` is actually unchecked (in which case #2 is moot but the naming is misleading and the `throws` clause is noise). Worth resolving before anything else, since the review conclusions depend on which it is.

## 4. External call inside the transaction, after the DB mutation

Two separate problems:

- **Lock/connection held across a network call.** The gateway call happens with an open transaction and a checked-out pooled connection. In a batch, the pool and the DB write locks are held for the sum of all gateway latencies. Under load this exhausts the pool and blocks unrelated traffic.
- **No reconciliation window.** The write happens before the call, and the commit happens after. If the gateway succeeds but the commit then fails, money left the account with no record of it — unrecoverable without manual reconciliation.

The standard shape is a state machine plus an outbox: commit `REFUND_PENDING`, call the gateway outside the transaction, then commit the terminal state, with a sweeper that reconciles rows stuck in `REFUND_PENDING` against the provider.

## 5. No idempotency key — retries double-refund

`gateway.refund(o.getPaymentId())` carries nothing to dedupe on. A client retry of `refundAll`, a container restart mid-batch, or a gateway-side timeout-then-success all produce a second refund. Pass a deterministic idempotency key (order id + refund attempt) and let the provider dedupe.

## 6. No guard on current order status

Nothing checks whether the order is refundable. An order already `REFUNDED`, or `CANCELLED`, or never captured, gets a gateway refund attempt anyway. Combined with #5, re-running a batch refunds everything a second time.

## 7. Concurrent refunds of the same order race

Two callers with overlapping `orderIds` both read the order, both see a refundable status, both call the gateway. Needs `@Lock(PESSIMISTIC_WRITE)` on the lookup, an `@Version` column, or a conditional update (`UPDATE ... WHERE status = 'PAID'`) whose affected-row count gates the gateway call.

## 8. Partial-batch failure is unreported

Once #1 is fixed, the first `PaymentException` aborts the loop. Orders before it are committed, orders after it are untouched, and the caller gets an exception with no indication of which. Collect per-order outcomes and return a result object; decide explicitly whether the batch is fail-fast or best-effort.

## 9. Unbounded batch, N+1 lookups

`List<Long>` has no size cap, and each iteration issues its own `findById`. Use `findAllById` for the load, cap the input (or page it), and make the gateway calls bounded-concurrent rather than serial if latency matters.

## 10. Bare `orElseThrow()`

Throws `NoSuchElementException` with no message — no order id, no domain meaning, surfaces as a 500. Use `orElseThrow(() -> new OrderNotFoundException(id))`.

---

The first two are the ones I'd block a merge on: together they mean the method can mark orders refunded without refunding them, which is exactly the failure mode the `REQUIRES_NEW` was presumably added to prevent.

</details>

<details><summary>Last output with the plugin</summary>

Using `backend-skills:transaction-boundaries` as the review lens.

## Problems, most serious first

**1. Checked exception commits the "REFUNDED" status without refunding the money.**
Spring rolls back on unchecked exceptions only. `PaymentException` is checked, so when `gateway.refund(...)` fails, the preceding `o.setStatus(REFUNDED)` commits anyway. Result: the order is marked refunded in the database and the customer was never paid. There is no `rollbackFor`, so this is the default path, not an edge case.

**2. `REQUIRES_NEW` is silently ignored — self-invocation bypasses the proxy.**
`refundAll` calls `refundOne(id)` on `this`, so the transactional proxy is never involved. Every refund runs inside `refundAll`'s single outer transaction. The entire per-order isolation the annotation is there to provide does not exist, and nothing in the code signals that. This is the dangerous kind of bug: the annotation reads as if the design is correct.

**3. It does not compile.**
`refundOne` declares `throws PaymentException` (checked); `refundAll` neither catches it nor declares it. Whatever the intended error handling was, it isn't in this change.

**4. Remote call inside an open transaction, N times in a loop.**
`gateway.refund(...)` holds a pooled connection and the row lock on `orders` for the full round trip. With a list of 500 ids that is one transaction spanning 500 gateway calls — connection pool starvation, lock contention with anything else touching those orders, and a near-certain transaction timeout. There is also no visible timeout on the gateway client, so one hung call pins a connection indefinitely.

**5. No idempotency on the refund call.**
`gateway.refund(o.getPaymentId())` carries no idempotency key, and nothing checks whether the order is already `REFUNDED`. A retried request, a duplicate id in `orderIds`, or a re-run after the batch half-failed will refund the same payment twice. Real money, no guard.

**6. Read-then-write with no lock or version column.**
Two concurrent `refundAll` calls covering the same order both read `PAID`, both set `REFUNDED`, both call the gateway. Needs a pessimistic lock on the select or an optimistic `@Version`.

**7. Partial failure is unobservable and unrecoverable.**
First failure aborts the loop. The caller gets `void` and an exception, with no record of which ids succeeded, which failed, and which were never attempted. There is no way to safely retry the batch (see #5).

**8. `orElseThrow()` loses the id.**
A bare `NoSuchElementException` with no message, surfacing as a 500 for what is a bad input id. One unknown id also kills the whole batch.

## Shape that fixes it

Split the DB write from the gateway call and move the orchestration out of a transaction:

```java
// Orchestrator — NOT transactional. Separate bean, so the proxy applies.
public List<RefundResult> refundAll(List<Long> orderIds) {
    List<RefundResult> results = new ArrayList<>();
    for (Long id : new LinkedHashSet<>(orderIds)) {        // dedupe
        try {
            results.add(RefundResult.ok(id, refunder.beginRefund(id)));
        } catch (Exception e) {
            results.add(RefundResult.failed(id, e));        // keep going
        }
    }
    return results;
}
```

```java
// Transaction 1: claim the order and record the intent. No network call.
@Transactional(rollbackFor = Exception.class)
public UUID beginRefund(Long id) {
    Order o = orders.findByIdForUpdate(id)
        .orElseThrow(() -> new OrderNotFoundException(id));
    if (o.getStatus() == OrderStatus.REFUNDED) return o.getRefundKey();  // idempotent
    o.setStatus(OrderStatus.REFUND_PENDING);
    UUID key = UUID.nameUUIDFromBytes(("refund:" + id).getBytes(UTF_8));
    o.setRefundKey(key);
    outbox.add("refund-requested", new RefundRequested(id, o.getPaymentId(), key));
    return key;
}
```

The outbox relay then calls `gateway.refund(paymentId, idempotencyKey)` outside any order transaction, with a bounded timeout and retries, and a short transaction flips `REFUND_PENDING → REFUNDED` once the gateway acknowledges. The stable idempotency key makes relay retries and duplicate deliveries safe, and `REFUND_PENDING` rows are a queryable list of refunds in flight — which is what #7 is actually missing.

If the outbox is more machinery than this service warrants, the minimum viable version is still: `rollbackFor = Exception.class`, `refundOne` on a separate injected bean, the gateway call outside the transaction that writes the status, an idempotency key on the gateway call, and a per-item try/catch so one failure doesn't strand the rest of the batch.

</details>
