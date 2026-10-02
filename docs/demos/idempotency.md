<!-- result: baseline=3 with=2 runs=3 -->
# Demo: idempotency

Signal (regex, case-insensitive): `unique (constraint|index)|fingerprint|in-flight|different payload`

| Arm | Runs mentioning the signal |
|---|---|
| Without the plugin | 3 / 3 |
| With the plugin | 2 / 3 |

## Code reviewed

```java
@PostMapping("/webhooks/transfer-settled")
public ResponseEntity<Void> onSettled(@RequestBody SettlementEvent e) {
    Account a = accounts.findById(e.accountId()).orElseThrow();
    a.setBalance(a.getBalance().add(e.amount()));
    accounts.save(a);
    return ResponseEntity.ok().build();
}
```

<details><summary>Last output without the plugin</summary>

Review of `onSettled` — ordered by severity.

### 1. Not idempotent: duplicate delivery double-credits the account
Webhook providers guarantee *at-least-once* delivery. A retry (your 200 lost in transit, a timeout, a provider-side replay) re-runs `balance + amount`. This is a silent money-creation bug and the most likely one to actually fire in production.

Fix: dedupe on the provider's event ID with a DB uniqueness guarantee, not a `select`-then-`insert` check:

```java
try {
    processedEvents.insert(e.eventId());   // PK / unique index on event_id
} catch (DuplicateKeyException dup) {
    return ResponseEntity.ok().build();    // already applied, ack and drop
}
```
`SettlementEvent` must carry an event ID for this; if it doesn't, that's a contract gap to raise with the provider.

### 2. Lost update: read-modify-write with no locking and no transaction
Two concurrent settlements for the same account both read balance `100`, add `10` and `20`, and one write wins — `110` or `120` instead of `130`. There's also no `@Transactional`, so the load and the save are in separate transactions (entity is detached between them), and nothing is atomic with the dedupe insert in #1.

Fix: `@Transactional` on the handler plus one of — `@Version` optimistic locking with retry, `findByIdForUpdate` (`PESSIMISTIC_WRITE`), or best, push the arithmetic into the database:

```java
@Modifying
@Query("update Account a set a.balance = a.balance + :amt where a.id = :id")
int credit(@Param("id") UUID id, @Param("amt") BigDecimal amt);
```

### 3. No authenticity check on the request
Nothing here verifies the caller. If this path is excluded from auth (as webhook paths usually are), any party who can reach it can credit arbitrary balances by POSTing JSON. You need HMAC signature verification over the **raw** body plus a timestamp window to block replays.

Note this interacts with `@RequestBody`: Spring deserializes and the stream is consumed, so signature verification must happen in a filter/interceptor over the cached raw bytes (`ContentCachingRequestWrapper`), or you accept `String`/`byte[]` and deserialize yourself after verifying. Re-serializing the DTO to recompute the MAC does not work — byte-for-byte differences break it.

### 4. `orElseThrow()` produces a 500 and an infinite retry loop
An unknown account ID throws `NoSuchElementException` → 500 → the provider retries, forever, for an event that can never succeed. You're conflating permanent failures with retryable ones.

Fix: 4xx (or 200 + dead-letter + alert) for unknown account and malformed payloads; 5xx only for genuinely transient faults (DB down) where a retry can help.

### 5. No validation of amount, sign, or currency
- `@RequestBody` without `@Valid` — `e.amount()` may be `null` → NPE → 500 → retry loop (#4).
- A negative `amount` silently debits the account through a "settled" endpoint. If negatives are legitimate, there's no overdraft/floor check.
- `BigDecimal.add` ignores currency entirely. A USD settlement lands on a EUR balance with no error. It also carries the event's scale into the balance, so `10.000` vs `10.00` leaks inconsistent scale into persisted money.

Fix: `@Valid` + constraints on the DTO, assert `e.currency().equals(a.getCurrency())` before crediting, and normalize scale (`setScale(2, RoundingMode.UNNECESSARY)` or a `Money` type).

### 6. No ledger entry or audit trail
The balance is mutated in place with no immutable record of *why*. After this runs you cannot answer "which events make up this balance," cannot reconcile against the provider, and cannot resolve a dispute. For a financial balance, the mutation and an append-only ledger row must be written in the same transaction — ideally balance is derived from (or reconcilable against) the ledger.

### 7. No observability
No log line with the event ID / account / amount, no metric or counter. When balances drift, there is nothing to investigate with. Log on entry with the event ID, and count successes, duplicates, and rejections separately.

### 8. Synchronous processing in the webhook request
Minor relative to the above, but holding the provider's connection open for the full DB transaction risks their timeout — which triggers a retry you're not yet protected against. The robust shape is: verify → persist raw event → ack 200 → process asynchronously with the same idempotency key.

---

One caveat on scope: #3 and #7 may be handled by a filter and logging config outside this snippet. #1, #2, #4, and #5 are visible defects in the code as written.

</details>

<details><summary>Last output with the plugin</summary>

Using `backend-skills:idempotency` and `backend-skills:money-handling` as the review lenses.

Two caveats on what I can see: the types of `SettlementEvent.amount()` and `Account.getBalance()` aren't in the snippet, and neither is the security config. Findings 5 and 2 depend on those; flag them as "verify" if those are already handled elsewhere.

## 1. No idempotency — duplicate delivery double-credits the account
Webhook delivery is at-least-once. Providers retry on timeout, connection reset, or any non-2xx — and your own gateway may retry too. Every redelivery of the same settlement adds `e.amount()` again. This is real money, silently wrong, and nothing in the code can detect it after the fact.

The key must come from the event (the provider's event ID, not `accountId` and not something generated in the handler), with a unique constraint on `(scope, event_id)`. Because the side effect here is purely a database write, insert the key row in the *same transaction* as the balance update so they commit or roll back together. On conflict, return the stored response without re-applying.

## 2. No signature verification — the endpoint mints money for anyone who can reach it
As written this is an unauthenticated POST that credits an arbitrary `accountId` with an arbitrary `amount`. If this is reachable from the internet without a filter doing HMAC verification, it's a direct financial exploit, not a hardening gap.

Note the structural problem: verifying an HMAC requires the **raw request bytes**. `@RequestBody SettlementEvent` has already been deserialized, and re-serializing it will not reproduce the signed payload (key order, whitespace, numeric formatting). Verification has to happen in a filter or via `HttpEntity<byte[]>`/`ContentCachingRequestWrapper` — you cannot bolt it on inside this method body. Include a timestamp tolerance so a captured valid request can't be replayed days later.

## 3. Lost update — read-modify-write with no transaction and no locking
`findById` → `add` → `save` is a classic lost update. Two settlements for the same account arriving concurrently (very likely — batch settlement runs fan out) both read the same starting balance, and whichever saves second overwrites the first. One credit vanishes.

There's also no `@Transactional`, so the read and the write are in separate transactions with no consistent snapshot between them. Fix with one of: an atomic `UPDATE accounts SET balance = balance + :amount WHERE id = :id`, a `SELECT … FOR UPDATE` on the account row, or `@Version` optimistic locking with a bounded retry. The atomic UPDATE is the cheapest and removes the race entirely.

## 4. Mutating a balance column, with no ledger entry
The only record that this settlement happened is a changed number. There's no append-only entry recording event ID, amount, timestamp, and resulting balance — so when reconciliation disagrees with the provider next quarter, there is no way to tell which events were applied, nor to detect or repair a double-credit from finding 1. Money movements should be insert-only rows; the balance is either derived or updated in the same transaction as the entry.

## 5. Currency is never checked, and the balance's scale drifts
`a.getBalance().add(e.amount())` adds two amounts with no assertion that they're the same currency. If `amount` is a bare `BigDecimal` or `long`, nothing stops a JPY settlement from crediting a USD account at face value. An amount must travel with its currency, and mismatches must be rejected, not added.

Separately, `BigDecimal.add` preserves the larger scale, so repeated credits leave the stored balance at drifting scales (`100.5`, `100.50`, `100.500`). Set scale explicitly from `currency.getDefaultFractionDigits()` — never a hardcoded 2 — and compare balances with `compareTo`, never `equals`.

**Verify immediately:** if `amount` or `balance` is `double`/`float` anywhere in this chain — the record, the entity, the column, or the JSON — that outranks everything else on this list. Also confirm whether `SettlementEvent` can carry a negative amount for reversals; blind `add` would apply a reversal as a credit.

## 6. `orElseThrow()` on an unknown account causes an infinite retry loop
An unknown `accountId` throws `NoSuchElementException` → 500 → the provider treats it as retryable and redelivers, forever, until it gives up and drops the event or floods your dead-letter queue. But an unknown account is a *permanent* failure; retrying cannot fix it.

Decide the mapping deliberately: park the event in an unmatched/quarantine table and return 2xx so the provider stops, then handle it manually. The bare `orElseThrow()` also discards all context — no accountId, no event ID in the message.

## 7. No request validation
No `@Valid`, no null checks. A missing `amount` NPEs inside `add` and surfaces as a 500 — which, per finding 6, means it gets retried forever. A null `accountId`, a zero amount, or a negative amount all flow straight through to the balance.

## 8. No audit logging on a money-moving path
Nothing records that this event was applied: no event ID, account, amount, old balance, or new balance. Combined with finding 4, the first reconciliation break will be undiagnosable.

---

The ordering matters here: findings 1, 2, and 3 are each independently capable of producing a wrong balance in production, and none of them can be fixed from inside this method as structured. 2 needs a filter with raw-body access, 1 needs a key table plus a transaction boundary, 3 needs either a different write strategy or row locking. I'd treat this as needing a redesign of the handler rather than patches — happy to write that up if useful.

</details>
