<!-- result: baseline=3 with=3 runs=3 -->
# Demo: idempotency.hard

Signal (regex, case-insensitive): `fingerprint|different (request|payload|body)|payload (mismatch|differ)|same key (with|but)|key (is )?reused`

| Arm | Runs mentioning the signal |
|---|---|
| Without the plugin | 3 / 3 |
| With the plugin | 3 / 3 |

## Code reviewed

```java
@PostMapping("/transfers")
public ResponseEntity<TransferResponse> create(
        @RequestHeader("Idempotency-Key") String key,
        @RequestBody TransferRequest req) {
    var existing = keys.findById(key);
    if (existing.isPresent()) {
        return ResponseEntity.ok(existing.get().getResponse());
    }
    TransferResponse res = transfers.execute(req);
    keys.save(new IdempotencyKey(key, res));
    return ResponseEntity.ok(res);
}
```

<details><summary>Last output without the plugin</summary>

Reviewing the snippet as-is. Problems, worst first:

**1. Race condition — the check-then-act is not atomic, so you get double transfers.**
Two concurrent requests with the same key both hit `findById`, both see empty, both call `transfers.execute`. That's a double spend, and it's the exact failure the idempotency key exists to prevent. The retry storm from a client timeout is precisely when duplicate concurrent requests arrive, so this is the common case, not the rare one.
Fix: make the key row the lock. Insert it *first* (`INSERT ... ON CONFLICT DO NOTHING`, or save and catch `DataIntegrityViolationException` on the unique constraint) in an `IN_PROGRESS` state; the loser of that race returns `409 Conflict` rather than executing.

**2. Execute-then-save is not atomic either — a crash in between loses the key.**
If the process dies, the pod is evicted, or `keys.save` fails after `execute` succeeded, the money moved but no key was recorded. The next retry re-executes. There is no `@Transactional` here at all. Transfer effect and key record must commit together; if `execute` hits an external rail that can't join the DB transaction, you need the durable intent row written *before* the external call (which is also what fixes #1).

**3. The key is globally scoped — cross-tenant data leak.**
`keys.findById(key)` looks up by the raw header value with no caller dimension. A client sending `"1"`, `"test"`, or any colliding value gets back *another tenant's* `TransferResponse` — amounts, account identifiers, the lot. Also lets one client silently suppress another's transfer. The identity must be `(principal/api-key, idempotency-key)`.

**4. No request fingerprint — key reuse with a different body silently swallows a real transfer.**
Reusing a key with a different payload returns the *first* response. A client bug that reuses a key for a $500 transfer after a $5 one gets back the $5 response and believes it succeeded. Store a hash of the canonicalized request body with the key and return `422`/`409` on mismatch.

**5. No validation on either input.**
No `@Valid` on `TransferRequest`. No constraint on the key: an arbitrary-length header goes straight into a DB primary key, so an oversized value becomes a `DataIntegrityViolationException` → 500 instead of 400. Unbounded key cardinality is also a cheap storage-exhaustion vector. Enforce a max length and charset, and reject blank.

**6. Failures are never recorded, and failed results may be cached forever.**
If `execute` throws, nothing persists, so every retry re-attempts — fine for a transient timeout, wrong for a deterministic rejection, and the code can't tell them apart. Conversely if `execute` *returns* a declined `TransferResponse`, that decline is cached permanently with no way to ever retry that key.

**7. Always `200 OK`, with no way to distinguish replay from creation.**
A created transfer should be `201` with a `Location` header; the replay path should be distinguishable (status, or an `Idempotent-Replay` header). As written a client can't tell whether it just moved money or read back a week-old record.

**8. No TTL or retention on the key store.**
The table grows without bound, and because the expiry window isn't defined, clients have no contract for how long a retry is safe. Pick a window (24h–7d), document it, and reap.

**9. Storing the response object is a versioning trap.**
`new IdempotencyKey(key, res)` persists the DTO. Change `TransferResponse`'s shape and stored rows fail to deserialize — a latent production break on deploy, not on the PR. Store versioned JSON, not a serialized Java object.

**10. Idempotency logic lives in the controller.**
This is a cross-cutting concern; every new mutating endpoint will copy-paste it, and the bugs above with it. It belongs in a filter/interceptor or behind the service's transactional boundary. It's also untestable where it sits — you can't unit-test the race.

**11. No observability.**
Nothing logs or meters replay-vs-fresh. The first duplicate-payment incident will be very hard to reconstruct without it.

The top two are the ones I'd block the PR on — both are live double-spend paths, not theoretical.

</details>

<details><summary>Last output with the plugin</summary>

Using the idempotency skill as the review lens.

Reviewing the snippet as written; a few points depend on code not shown (whether the method is `@Transactional`, what `keys` is backed by, what `transfers.execute` does internally) — I've flagged those as conditional.

## 1. `findById` then `save` is a race — concurrent duplicates both transfer money
Two requests with the same key arriving at the same time (client retry while the first is still in flight, or a gateway retry) both find `existing` empty, both call `transfers.execute`, and money moves twice. This is the most likely failure in production, because retries happen *precisely* when the first request is slow and still running.

The key must be claimed by the database, not checked by the application: insert `(scope, key)` with a unique constraint and `status = IN_PROGRESS` **before** doing any work, and let the conflict tell you a duplicate exists.

## 2. The key is saved only after the work succeeds — crash window causes double transfer
If the process dies, the pod is evicted, or the DB connection drops between `transfers.execute(req)` returning and `keys.save(...)` committing, the transfer has happened and no key exists. The retry sees nothing and transfers again. Same outcome if `keys.save` itself fails (constraint, timeout, serialization failure): the transfer is already done and now unrecoverably unrecorded.

Claim-before-work (`IN_PROGRESS` row + lease), then mark `DONE` with the response as the last step.

## 3. The key is not scoped to the caller — cross-account response leak
`keys.findById(key)` treats the key as globally unique. Clients choose these values, and they choose badly: `"1"`, `"transfer-1"`, a UUID that got copy-pasted, a retry of a value from a test script. When caller B sends a key caller A already used, B gets back **A's** `TransferResponse` — another account's transfer details — and B's transfer never executes. That's both a data disclosure and a silently dropped payment.

Primary key should be `(scope, key)` where scope includes the authenticated principal (and ideally the endpoint), not `key` alone.

## 4. No request fingerprint — same key, different payload returns the wrong answer
A client that reuses a key for a genuinely different transfer (classic bug: key generated once per session/page rather than per operation) gets a `200 OK` with the *previous* transfer's response. The caller believes "send $5,000 to Bob" succeeded; nothing happened. Store a fingerprint (hash of the canonicalized body, plus the scoped key's relevant fields) and reject a mismatch with `409`/`422` instead of replaying.

## 5. No in-flight state, so a duplicate can't be answered correctly
Even after fixing #1 with a unique constraint, you need to distinguish *done* from *in progress*. Without it, the second request either starts a parallel copy of the work or gets a confusing error. With a lease (`locked_until`), the behavior is defined: `DONE` → replay the stored response; `IN_PROGRESS`, lease live → `409 Conflict` / `425`-style "processing"; lease expired → one conditional `UPDATE ... WHERE locked_until < now()` lets exactly one retry take over. Make the lease longer than the transfer's own timeout, or renew it, so you never take over work that's still running.

## 6. No key is passed downstream — `transfers.execute(req)` isn't idempotent either
Whatever `transfers.execute` does — ledger debit, credit, payment-rail call, outbound message — carries no idempotency token. So the moment a takeover or a retry-after-partial-failure happens, the downstream side effects repeat. Pass a derived key (`key + ":debit"`, `key + ":credit"`, …) so each step is individually deduplicated at its own boundary, and make every step inside `execute` safe to repeat.

## 7. `keys.save` on a conflict may silently overwrite rather than fail
If `keys` is a Spring Data JPA repository, `save` on an entity whose ID already exists performs a merge/update, not an insert failure. In the race from #1, the loser overwrites the winner's stored response, so the two concurrent callers can end up with the same stored response for two distinct transfers that both executed. Use an explicit `INSERT ... ON CONFLICT DO NOTHING` and branch on the affected row count — don't rely on `save` to detect duplication.

## 8. Failures aren't recorded at all
If `transfers.execute` throws, the exception propagates and no row is written. For a clean "rejected: insufficient funds" that's arguably fine (the retry re-validates). But for an ambiguous failure — timeout, connection reset after the downstream received the request — you've lost all record of an operation that may have partially succeeded, and the retry has no way to resume it. Decide explicitly which failures leave the key claimed (and resumable) versus released.

## 9. Transaction boundary
Not visible here, but if this handler (or `transfers.execute`) runs inside a single `@Transactional`, the external calls happen while a DB transaction is open — the key row stays invisible to concurrent requests until commit, which defeats the claim in #1, and the transaction is held open for the duration of a network call. The claim must commit in its own short transaction; the work runs outside any open transaction.

## 10. HTTP semantics and response fidelity
- Always `200 OK`, including on first creation — a resource-creating endpoint should return `201` with a `Location` header on first execution, and the replay should reproduce the original status, not flatten everything to `200`.
- Only the body is stored. Status code and relevant headers aren't, so a replay can't faithfully reproduce the original response.
- Consider marking replays (e.g. `Idempotent-Replay: true`) — useful for client debugging and for your own metrics on retry volume.

## 11. The key itself is unvalidated attacker-controlled input
`@RequestHeader` makes it required (missing → `400`), which is right, but there's no check for empty/whitespace-only, no maximum length, and no character-set constraint. An unbounded header value goes straight into a primary key. Validate: non-blank, bounded length (e.g. ≤ 255), reject on violation with `400`.

## 12. No stated retention or cleanup
Keys must outlive the longest retry window — client retry budgets, gateway retries, and any manual replay window — so pin a TTL (commonly 24h–7d for money movement) and add a cleanup job. Nothing here says how long rows live, and an unbounded `idempotency_keys` table on a transfers endpoint grows forever.

---

**Shape of the fix:** extract this into a reusable `idempotency.execute(scope, key, fingerprint, work)` helper rather than inlining it per endpoint — claim via `INSERT ... ON CONFLICT` with `IN_PROGRESS` + lease in its own transaction, run `work` outside any transaction with derived keys passed downstream, then store status/body and mark `DONE`. Table as:

```sql
CREATE TABLE idempotency_keys (
    scope        text        NOT NULL,
    key          text        NOT NULL,
    fingerprint  text        NOT NULL,
    status       text        NOT NULL,   -- IN_PROGRESS | DONE
    locked_until timestamptz,
    status_code  int,
    response     jsonb,
    created_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (scope, key)
);
```

The single highest-value change is moving the key claim *before* the work and letting a unique constraint enforce it — that alone closes #1 and #2, which are the two paths that move money twice.

</details>
