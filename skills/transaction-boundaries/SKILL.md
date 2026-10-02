---
name: transaction-boundaries
description: Use when writing or reviewing code that mixes database writes with external side effects such as HTTP calls, message publishing, emails, or cache updates
---

# Transaction boundaries

A database transaction can roll back. An HTTP call, a published message, or a sent email cannot. Mixing them inside one method is how systems end up charged-but-not-ordered or announced-but-not-saved.

## When to use

- A method writes to the database and also calls another service, publishes an event, sends a notification, or updates a cache.
- Reviewing any `@Transactional` method.

## Checklist

1. No network call inside an open transaction. It holds a connection and locks while it waits, and it cannot be undone if the commit fails.
2. Publishing before commit can announce something that then rolls back. Publishing after commit can be lost if the process dies in between. Use a transactional outbox: write the event as a row in the same transaction, and let a relay publish it.
3. Assume every consumer sees events at least once, and make handlers idempotent (see `idempotency`).
4. With Spring's default proxy mode, `@Transactional` is applied by a proxy. A call from another method in the same class bypasses it, and so does a private method.
5. By default Spring rolls back on unchecked exceptions only. A checked exception commits unless `rollbackFor` says otherwise.
6. Keep transactions short. No waiting on users or remote systems.
7. A read followed by a write of the same row needs a lock or an optimistic version column, or two requests will overwrite each other.

## Bad example

```java
@Transactional
public void placeOrder(Order o) {
    orders.save(o);
    paymentClient.charge(o.total());        // remote call inside the transaction
    broker.send("order-placed", o.id());    // published before commit
}
```

## What to flag

- `charge` runs even if the commit later fails: the customer is charged and there is no order.
- `send` can be consumed before the commit, so a consumer looks up an order that does not exist yet. If the commit fails, the event describes something that never happened.
- The remote call keeps a database connection and row locks open for as long as the payment service takes to answer.

## Good example

```java
@Transactional
public void placeOrder(Order o) {
    orders.save(o);
    outbox.add("order-placed", o.id());   // same transaction as the order
}

// Separate component, runs after commit and retries on failure.
@Scheduled(fixedDelay = 500)
void relay() {
    for (OutboxEvent e : outbox.pending(100)) {
        broker.send(e.topic(), e.payload());   // must block until the broker acknowledges
        outbox.markSent(e.id());
    }
}
```

The order and its event commit or roll back together. `send` must block until the broker acknowledges, otherwise `markSent` can record an event that was never delivered. The relay can still publish an event twice if it crashes between `send` and `markSent`, which is why consumers must be idempotent. If several relay instances run, claim rows with `FOR UPDATE SKIP LOCKED` or run a single relay. Charge the customer in a consumer of `order-placed`, using an idempotency key derived from the order ID.
