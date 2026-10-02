---
name: idempotency
description: Use when writing or reviewing an endpoint, webhook, message consumer, or job that can be retried or delivered more than once, especially if it creates records, moves money, or calls another service
---

# Idempotency

An operation is idempotent when running it twice has the same effect as running it once. Anything reachable over a network will be retried: clients time out, gateways retry, brokers redeliver, schedulers re-run, users double-click.

## When to use

- Writing or reviewing a POST or PUT endpoint, webhook receiver, queue consumer, or scheduled job.
- The operation creates a record, changes a balance, sends a message, or calls a downstream service.

## Checklist

1. Name the retry sources for this code path: client, gateway, at-least-once broker, scheduler, user.
2. The key comes from the caller or from the message ID. It is never generated inside the handler.
3. Uniqueness is enforced by the database, with a unique constraint on `(scope, key)`. Insert the key first, in the same transaction as the side effect. "SELECT, then INSERT" is a race.
4. Store a fingerprint of the request. The same key with a different payload is a client bug: reject it with 409 or 422.
5. Store the result, so a replay returns the original response instead of running again.
6. Handle the in-flight duplicate: a second request arriving while the first is still running must wait or get a "processing" answer. It must never run twice.
7. Keep keys longer than the longest possible retry window.
8. Pass a derived key downstream (for example `key + ":debit"`), so the calls you make are idempotent too.

## Bad example

```java
@PostMapping("/payments")
Payment create(@RequestBody PaymentRequest req) {
    Payment p = payments.save(new Payment(req.accountId(), req.amount()));
    ledger.debit(req.accountId(), req.amount());
    return p;
}
```

## What to flag

- No idempotency key. If the client times out after `save` and retries, a second payment is created.
- `ledger.debit` carries no key, so a retried request can debit twice.
- A crash between `save` and `debit` leaves a payment with no debit, and a retry creates a second payment instead of finishing the first.

## Good example

```java
@PostMapping("/payments")
Payment create(@RequestHeader("Idempotency-Key") String key,
               @RequestBody PaymentRequest req) {
    return idempotency.execute("payments", key, req.fingerprint(), () -> {
        Payment p = payments.save(new Payment(req.accountId(), req.amount()));
        ledger.debit(req.accountId(), req.amount(), key + ":debit");
        return p;
    });
}
```

```sql
CREATE TABLE idempotency_keys (
    scope       text        NOT NULL,
    key         text        NOT NULL,
    fingerprint text        NOT NULL,
    status      text        NOT NULL,  -- IN_PROGRESS or DONE
    response    jsonb,
    created_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (scope, key)
);
```

`idempotency.execute` inserts the row first. A conflict means a replay (return the stored response if `DONE`) or an in-flight duplicate (wait or answer "processing"). A fingerprint mismatch is rejected. After the block runs it stores the response. Keep remote calls like `ledger.debit` outside any open database transaction; see `transaction-boundaries`.
