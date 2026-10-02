<!-- result: baseline=3 with=3 runs=3 -->
# Demo: safe-migrations.hard

Signal (regex, case-insensitive): `NOT VALID|separate (transaction|migration)|lock_timeout|in batches|batched|expand and contract`

| Arm | Runs mentioning the signal |
|---|---|
| Without the plugin | 3 / 3 |
| With the plugin | 3 / 3 |

## Code reviewed

```sql
-- V42__add_status.sql  (Flyway, runs in a single transaction)
ALTER TABLE payments ADD COLUMN status text;
UPDATE payments SET status = 'NEW';
ALTER TABLE payments ALTER COLUMN status SET NOT NULL;
ALTER TABLE payments ADD CONSTRAINT payments_status_chk CHECK (status IN ('NEW', 'DONE'));
CREATE INDEX CONCURRENTLY idx_payments_status ON payments (status);
```

<details><summary>Last output without the plugin</summary>

## Blocking

**1. The migration will fail outright — `CREATE INDEX CONCURRENTLY` cannot run inside a transaction block.**
Flyway wraps this file in one transaction, so Postgres aborts with `ERROR: 25001: CREATE INDEX CONCURRENTLY cannot run inside a transaction block`. Everything rolls back and Flyway marks V42 failed, requiring `flyway repair` before you can deploy again. The concurrent index must live in its own migration with `executeInTransaction=false` (script config or `-- flyway:executeInTransaction=false` depending on your Flyway version). Mixing it with transactional DDL is not fixable by reordering.

**2. Blanket `SET status = 'NEW'` is almost certainly data corruption.**
Every historical payment — settled, refunded, failed — gets stamped `NEW`. On a payments table that is the kind of error that causes money to move twice if any job polls `WHERE status = 'NEW'`. The backfill must derive state from existing columns (`settled_at`, `captured_at`, whatever exists), and rows that can't be classified need an explicit terminal/unknown value, not the initial one.

**3. `NOT NULL` with no `DEFAULT` breaks every existing `INSERT` that omits the column.**
During a rolling deploy, old pods are still issuing inserts without `status` and will start erroring the moment this commits. Same for any ETL, admin script, or ORM mapping not yet updated. Backward-compatible column addition needs a default that survives until all writers are updated.

## Serious

**4. The whole table is locked `ACCESS EXCLUSIVE` for the entire backfill — reads included.**
`ADD COLUMN` takes `ACCESS EXCLUSIVE` and holds it until commit, so the `UPDATE`, the `SET NOT NULL` scan, and the `CHECK` validation all happen under a lock that blocks selects, not just writes. On PG 11+ this is unnecessary: `ADD COLUMN status text NOT NULL DEFAULT 'NEW'` is a metadata-only fast default and collapses steps 1–3 into one near-instant operation.

**5. The `UPDATE` rewrites every row.**
One new tuple version per row: table size roughly doubles until autovacuum catches up, plus a WAL spike proportional to table size that will push replication lag and archive pressure. If you genuinely need a value-derived backfill (see #2), it has to be batched outside the schema migration, not a single statement.

**6. No `lock_timeout`.**
If any long-running query or idle-in-transaction session holds a conflicting lock, the `ALTER TABLE` queues — and every subsequent query on `payments` queues behind it. That's a self-inflicted outage. Set `SET lock_timeout = '3s'` (and `statement_timeout`) at the top and retry on failure rather than waiting indefinitely.

**7. `SET NOT NULL` does a full table scan that you could have avoided.**
PG 12+ can skip it if a validated `CHECK (status IS NOT NULL)` already exists, but your check is added *after* and constrains values, not nullability. Moot if you adopt the fast-default approach in #4.

## Worth fixing

**8. The index is the wrong shape and probably useless.**
A full btree over a two-value column won't be chosen by the planner for the common value, and it adds write amplification on every payment insert/update. If the access pattern is "find work to do," you want `CREATE INDEX CONCURRENTLY ... ON payments (status) WHERE status = 'NEW'` — or a composite with whatever you actually filter/sort by alongside it.

**9. A two-value closed `CHECK` is too narrow for a payment lifecycle.**
No `PENDING`, `FAILED`, `REFUNDED`, `CANCELLED`. Every added value later means a constraint swap, and done naively that's another full-table validation scan under an exclusive lock. If you keep the pattern, future changes should use `ADD CONSTRAINT ... NOT VALID` followed by a separate `VALIDATE CONSTRAINT`, which takes only `SHARE UPDATE EXCLUSIVE`.

**10. Not re-runnable after a partial failure.**
No `IF NOT EXISTS` anywhere, and once you split out the concurrent index, a failed `CREATE INDEX CONCURRENTLY` leaves an `INVALID` index behind that must be dropped before retrying. That cleanup path needs to be written down, because it will happen.

## Shape of the fix

```sql
-- V42__add_status.sql  (transactional)
SET lock_timeout = '3s';
ALTER TABLE payments
  ADD COLUMN status text NOT NULL DEFAULT 'NEW';   -- metadata-only on PG 11+
ALTER TABLE payments
  ADD CONSTRAINT payments_status_chk
  CHECK (status IN ('NEW','PENDING','DONE','FAILED')) NOT VALID;
```
```sql
-- V43__validate_status.sql
ALTER TABLE payments VALIDATE CONSTRAINT payments_status_chk;
```
```sql
-- V44__index_status.sql   + executeInTransaction=false
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_payments_status_new
  ON payments (status) WHERE status = 'NEW';
```

The real backfill for pre-existing rows (#2) belongs in a batched job, and the `DEFAULT` should only be dropped once every writer sets `status` explicitly.

One thing I can't judge from the diff: whether `payments` already has a column encoding lifecycle state. If it does, this is a redundant denormalization and the review conversation should be about that instead.

</details>

<details><summary>Last output with the plugin</summary>

Using **backend-skills:safe-migrations** to review this against the Postgres migration checklist.

## Problems, most serious first

**1. `CREATE INDEX CONCURRENTLY` cannot run inside a transaction block — this migration fails outright.**
Flyway wraps each migration in one transaction on Postgres by default, so statement 5 aborts with `25001: CREATE INDEX CONCURRENTLY cannot run inside a transaction block`, rolling back the whole file. This never deploys. The index must live in its own migration file marked `executeInTransaction=false` (script config `V43__index_payments_status.sql.conf`). Don't just set that flag on *this* file — you'd lose atomicity for the DDL above, and a mid-file failure leaves a half-migrated schema plus a failed schema-history row needing `flyway repair`. Also: a failed concurrent build leaves an `INVALID` index behind that must be dropped before retrying.

**2. The table is exclusively locked for the entire duration of the backfill — this is the outage.**
`ADD COLUMN` takes `ACCESS EXCLUSIVE` on `payments` and, because everything is one transaction, holds it until commit. So the full-table `UPDATE`, the `SET NOT NULL` scan, and the `CHECK` validation scan all happen with reads *and* writes blocked. On a real payments table that is minutes, not seconds. Separately, the one-shot `UPDATE` rewrites every row in a single transaction: the table roughly doubles in size (bloat that only `VACUUM FULL`/`pg_repack` reclaims), generates WAL proportional to the whole table, and stalls replicas. Backfills belong outside the schema migration, in throttled, resumable keyset batches.

**3. Backfilling every existing row to `'NEW'` is almost certainly wrong data, not just slow.**
`payments` predates this column, so it contains already-settled and failed payments. Stamping all of them `'NEW'` tells every downstream consumer that historical payments are unprocessed — if anything polls `WHERE status = 'NEW'` (and the index you're adding suggests something will), you risk re-processing or re-paying settled rows. Derive the initial value from existing state (`settled_at`, etc.) or introduce the column with no assumed meaning, rather than a blanket literal.

**4. No `lock_timeout` or `statement_timeout`.**
The first `ALTER TABLE` queues for `ACCESS EXCLUSIVE` behind any long-running query or idle-in-transaction session — and once it's queued, every subsequent reader and writer queues behind *it*. One slow analytics query turns this migration into a full table stall. Start each migration with `SET lock_timeout = '3s';` so it fails fast and retries instead of forming a lock convoy.

**5. `SET NOT NULL` and the `CHECK` land in the same release that adds the column — rolling deploys and rollback break.**
During the deploy window, old application instances are still inserting `payments` rows without `status`; those inserts now fail with a not-null violation, and they'd keep failing if you roll the app back. This violates expand-and-contract: add the column, ship code that writes it, *then* enforce. Either stage the constraint into a later release or give the column `DEFAULT 'NEW'` so old code's inserts remain valid.

**6. `SET NOT NULL` does a full table scan under the strong lock, and it's in the wrong order to avoid it.**
Postgres 12+ skips that scan if a *validated* `CHECK (status IS NOT NULL)` already exists — but here `SET NOT NULL` runs *before* any check, and the check you do add (`status IN (...)`) isn't the `IS NOT NULL` form, so the optimization can't fire either way. Add `CHECK (status IS NOT NULL) NOT VALID`, validate it in a separate transaction, then `SET NOT NULL`, then drop the helper constraint.

**7. `ADD CONSTRAINT ... CHECK` without `NOT VALID` validates immediately.**
That's another full scan while holding `ACCESS EXCLUSIVE`. Split into `ADD CONSTRAINT ... NOT VALID` and a separate-transaction `VALIDATE CONSTRAINT` — validation takes a weaker lock that doesn't block reads or writes, *but only if it commits separately*. Flyway's per-migration transaction defeats this, so it must be two migration files, not two statements.

**8. No rollback stated.** Nothing says what happens if this needs to be undone. The honest answer is that it's forward-only (you can't drop the column in the same release that starts using it), but that should be written down rather than left implicit.

**9. No evidence of timing against production-sized data.** Every item above is size-dependent. "Row count of `payments` and measured duration on a prod-sized copy" is the missing input that decides whether this is a 10-second migration or a 20-minute one.

**10. Minor: the index is likely the wrong shape.** `status` has two values, so a plain btree over the whole column is poorly selective and Postgres will often seq-scan anyway. If the access pattern is a worker draining pending rows, a partial index (`... (id) WHERE status = 'NEW'`) is far smaller and actually gets used. Also consider a lookup table or enum over `text` + `CHECK` — every new state in a `CHECK` means another `ALTER` with a validation scan.

## Shape of the fix

```sql
-- V42__add_payments_status.sql   (transactional; metadata-only on PG 11+)
SET lock_timeout = '3s';
ALTER TABLE payments ADD COLUMN status text DEFAULT 'NEW';   -- nullable for now; no rewrite

-- Deploy app code that writes `status` on every insert/update. Then:

-- V43__backfill_payments_status  -> NOT a schema migration.
-- Throttled, resumable keyset batches, deriving the real value from settled_at et al.

-- V44__payments_status_not_null.sql
SET lock_timeout = '3s';
ALTER TABLE payments ADD CONSTRAINT payments_status_nn CHECK (status IS NOT NULL) NOT VALID;

-- V45__validate_status_nn.sql    (separate transaction: weak lock, no blocking)
SET lock_timeout = '3s';
ALTER TABLE payments VALIDATE CONSTRAINT payments_status_nn;

-- V46__payments_status_enforce.sql
SET lock_timeout = '3s';
ALTER TABLE payments ALTER COLUMN status SET NOT NULL;        -- PG 12+: no scan
ALTER TABLE payments DROP CONSTRAINT payments_status_nn;
ALTER TABLE payments ADD CONSTRAINT payments_status_chk CHECK (status IN ('NEW','DONE')) NOT VALID;

-- V47__validate_status_chk.sql
SET lock_timeout = '3s';
ALTER TABLE payments VALIDATE CONSTRAINT payments_status_chk;

-- V48__index_payments_status.sql  + V48__index_payments_status.sql.conf
--   containing: executeInTransaction=false
CREATE INDEX CONCURRENTLY idx_payments_status ON payments (id) WHERE status = 'NEW';
```

The one judgment call I'd escalate before any of this: whether `'NEW'` is a safe value for pre-existing rows (problem 3). If it isn't, the backfill logic — not the migration mechanics — is the part that needs design work.

</details>
