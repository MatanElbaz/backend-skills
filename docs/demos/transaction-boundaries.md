<!-- result: baseline=3 with=3 runs=3 -->
# Demo: transaction-boundaries

Signal (regex, case-insensitive): `outbox|after (the )?commit|before (the )?commit`

| Arm | Runs mentioning the signal |
|---|---|
| Without the plugin | 3 / 3 |
| With the plugin | 3 / 3 |

## Code reviewed

```java
@Transactional
public void register(User u) {
    users.save(u);
    emailClient.sendWelcome(u.email());
    events.publish(new UserRegistered(u.id()));
}
```

<details><summary>Last output without the plugin</summary>

Reviewing the snippet as-is (Spring `@Transactional`, default settings assumed).

## 1. Irreversible side effects inside a transaction (dual-write bug)
`sendWelcome` and `publish` are not transactional. If the commit fails — constraint violation on flush, deadlock, connection drop, or `events.publish` throwing after the email went out — the row is rolled back but the email is already delivered and the event may already be on the broker. You get welcome emails and `UserRegistered` events for users that don't exist. This is the most serious issue: it's silent and unrecoverable.

## 2. The inverse: a transient email outage fails registration
Because the call is inside the transaction and `sendWelcome` presumably throws a `RuntimeException`, an SMTP blip rolls back a perfectly valid registration. A non-critical notification is now on the critical path. Registration availability becomes `min(db, smtp, broker)`.

## 3. Checked exception from `emailClient` commits a half-done registration
Spring's default rollback rule is `RuntimeException`/`Error` only. If `sendWelcome` declares a checked exception, the transaction **commits**: user saved, email sent, event never published. Downstream consumers never learn about the user, and nothing in the logs says the state is inconsistent. Needs `rollbackFor` — or better, fix #1 so it's moot.

## 4. DB connection pinned across two network round-trips
The transaction (and its pooled connection) stays open for the duration of an SMTP call and a broker publish. Under load this exhausts the pool long before the DB is the bottleneck, and tail latency on the email provider turns into cascading timeouts across unrelated endpoints. Transactions should not contain remote I/O.

## 5. `u.id()` may be null
The ID is read off the argument, not off `users.save(u)`'s return value. With a DB-generated ID and no flush, or when Spring Data routes a non-new entity through `merge`, the returned instance is the managed one and the passed-in `u` can be unpopulated. Use `var saved = users.save(u); ... saved.id()`.

## 6. Not idempotent
Two concurrent or retried calls produce two emails and two events. There's no unique constraint check on email surfaced here, no dedupe key on the event, and no event ID for consumers to deduplicate against. Retries at any layer (client, gateway, message redelivery) multiply the user-visible damage.

## 7. Observability and error handling are absent
No logging of which step failed, no metrics, no timeout on the email client, no retry/backoff. When #1 happens in production you'll have no way to reconstruct who got an orphaned email.

## 8. Minor
- `void` return means the caller can't get the generated ID without a second lookup.
- No null/validation guard on `u` or `u.email()`.
- If `register` is ever called from within the same class, the proxy is bypassed and there is no transaction at all.
- Default propagation `REQUIRED` means this can silently join a caller's wider transaction, widening the blast radius of #4.

## Suggested shape

```java
@Transactional
public UserId register(User u) {
    var saved = users.save(u);                     // DB only
    outbox.enqueue(new UserRegistered(saved.id()));// same transaction
    return saved.id();
}
```

Then publish from the outbox after commit, and make the welcome email a consumer of `UserRegistered` (with retry + idempotency key) rather than an inline call. If you don't want an outbox table, the minimum viable fix is `@TransactionalEventListener(phase = AFTER_COMMIT)` for both side effects — that fixes #1–#4 but still loses events if the process dies between commit and dispatch.

</details>

<details><summary>Last output with the plugin</summary>

Using backend-skills:transaction-boundaries and backend-skills:idempotency to review this.

## Problems, worst first

**1. `sendWelcome` is an unrecoverable side effect inside a revocable transaction.** If the commit fails after the email goes out — constraint violation on a later statement, connection drop, deadlock, pool timeout — the user receives "welcome" for an account that does not exist. You cannot un-send an email. Any outcome where the DB rolls back and the mail is already delivered is permanently inconsistent.

**2. `events.publish` runs before commit.** A consumer can receive `UserRegistered` and look up `u.id()` before the insert is visible, so the handler fails or (worse) sees a half-built user. And if the transaction rolls back, you have published an event describing something that never happened. Note the ambiguity: if `events` is Spring's `ApplicationEventPublisher` with default synchronous listeners, those listeners run *inside* this transaction and inherit every problem on this list; if it's a broker, the lost/premature-publish problem applies. Either way it's wrong here.

**3. A transient SMTP outage fails registration.** The welcome email is non-critical, but it's on the critical path and inside the rollback scope. Mail provider has a bad five minutes → nobody can sign up. The criticality coupling is backwards.

**4. Network I/O holds a DB connection and row locks for the duration of two remote calls.** Transaction length is now bounded by your mail provider's p99, not your own code. Under load this exhausts the connection pool and the pool timeouts cascade into unrelated endpoints.

**5. No idempotency.** Retry sources here are real: client timeout + retry, gateway retry, user double-click. Each retry creates another user row and sends another welcome email. You need a unique constraint on `email` (so the duplicate is a DB-level failure, not a race) and an idempotency key claimed by insert before any work. "Check if email exists, then insert" is a race, so don't reach for that.

**6. `u.id()` may be null.** `users.save(u)` is called for its side effect and the return value is dropped. With Spring Data JDBC/Mongo the passed instance isn't mutated — the generated ID is only on the returned object — and with a JPA sequence/table generator the flush timing varies. `new UserRegistered(u.id())` can publish an event with a null ID. Use the value `save` returns.

**7. Rollback semantics aren't pinned.** Spring rolls back on unchecked exceptions only. If `sendWelcome` throws a checked exception, the transaction *commits*: user saved, no email, no event, and the caller sees a failure. Either declare `rollbackFor` or — better, per the fix below — remove the need for it.

**8. Proxy-mode footguns.** With default proxy AOP, `@Transactional` is applied by the proxy: any call to `register` from another method in the same class runs with no transaction at all, silently. Worth confirming the only callers are external.

## Fix

Write the event in the same transaction; do everything external outside it.

```java
@Transactional
public User register(User u) {
    User saved = users.save(u);                              // unique constraint on email
    outbox.add("user-registered", new UserRegistered(saved.id()));
    return saved;
}
```

A relay publishes outbox rows after commit, blocking until the broker acknowledges before marking them sent; `FOR UPDATE SKIP LOCKED` if more than one instance runs. The welcome email becomes a consumer of `user-registered`, keyed on the user ID so a redelivery doesn't mail twice — which it must be, since the relay can publish the same event twice if it crashes between send and mark-sent. Mail outages then delay the welcome email instead of breaking signup.

At the entry point, claim an idempotency key (`Idempotency-Key` header, or the message ID if this is driven by a queue) in its own short transaction before calling `register`, and store the response so a replay returns the original user rather than attempting a second insert.

</details>
