<!-- result: baseline=3 with=3 runs=3 -->
# Demo: safe-migrations

Signal (regex, case-insensitive): `NOT VALID|lock_timeout|keyset|in batches`

| Arm | Runs mentioning the signal |
|---|---|
| Without the plugin | 3 / 3 |
| With the plugin | 3 / 3 |

## Code reviewed

```sql
ALTER TABLE orders ALTER COLUMN customer_id SET NOT NULL;
CREATE UNIQUE INDEX uq_orders_ref ON orders (reference);
UPDATE orders SET region = 'EU' WHERE region IS NULL;
```

<details><summary>Last output without the plugin</summary>

Reviewing as Postgres (the `ALTER COLUMN ... SET NOT NULL` syntax is PG; MySQL would need `MODIFY COLUMN` and the locking notes differ).

## Blocking problems

**1. `SET NOT NULL` on `customer_id` will abort the migration if any NULLs exist.** There's a backfill for `region` but none for `customer_id`. If this table has any legacy rows with NULL customer, statement 1 fails and the whole migration rolls back. You need to check (`SELECT count(*) FROM orders WHERE customer_id IS NULL`) and decide: backfill, delete, or quarantine those rows — in a *separate, earlier* migration, not this one.

**2. `CREATE UNIQUE INDEX` will abort if duplicate `reference` values exist.** Same issue: no pre-check, no dedup step. Duplicates are likely exactly why someone wants this constraint.

**3. Ordering is wrong relative to the data fix.** The `region` backfill runs last, after two schema statements that can fail. Data remediation should precede the constraints that depend on it. As written, a failure in statement 1 or 2 also discards the region backfill.

## Locking / availability

**4. `SET NOT NULL` takes `ACCESS EXCLUSIVE` and full-scans the table** — all reads and writes on `orders` block for the duration. On a large orders table that's an outage. Use the two-phase pattern:
```sql
ALTER TABLE orders ADD CONSTRAINT orders_customer_id_not_null
  CHECK (customer_id IS NOT NULL) NOT VALID;
ALTER TABLE orders VALIDATE CONSTRAINT orders_customer_id_not_null;  -- SHARE UPDATE EXCLUSIVE
ALTER TABLE orders ALTER COLUMN customer_id SET NOT NULL;            -- PG12+: skips the scan
ALTER TABLE orders DROP CONSTRAINT orders_customer_id_not_null;
```

**5. `CREATE UNIQUE INDEX` without `CONCURRENTLY` blocks all writes for the entire build.** Use `CREATE UNIQUE INDEX CONCURRENTLY` — but note it cannot run inside a transaction block, so this migration must be split, and your migration tool must support non-transactional migrations. Also add handling for the failure mode: a cancelled `CONCURRENTLY` build leaves an `INVALID` index that must be dropped before retry.

**6. No `lock_timeout` / `statement_timeout`.** A queued `ACCESS EXCLUSIVE` request blocks every query behind it, so one long-running read turns this migration into a site-wide stall. Set `SET lock_timeout = '3s'` and retry rather than waiting indefinitely.

**7. The `UPDATE` is unbounded.** One statement rewriting every NULL-region row = one long transaction, held row locks, table bloat, WAL spike, replica lag. Batch it (`WHERE region IS NULL LIMIT 10000` in a loop with commits between).

## Correctness / design

**8. The region backfill is not durable — it's a point-in-time patch with no constraint behind it.** Nothing stops new NULL `region` rows the moment the statement commits (including rows inserted *during* the migration). If the goal is "region is always set," you need a `DEFAULT 'EU'` and/or `NOT NULL`. If it isn't, this UPDATE is a one-off data edit that will need repeating — it doesn't belong in a schema migration.

**9. Hardcoded `'EU'` silently relabels unknown-region orders.** This is an irreversible business-data assertion: after it runs you cannot distinguish a genuine EU order from an unknown one. If region drives tax, pricing, or GDPR handling, that's a compliance problem, not just a data one. Prefer an explicit `'UNKNOWN'` sentinel, or record the backfilled set first.

**10. Unique index on `reference` allows multiple NULLs.** In Postgres, NULLs are distinct by default, so unlimited NULL references remain legal. If `reference` is meant to be mandatory and unique, add `NOT NULL` too (or `NULLS NOT DISTINCT` on PG15+). Also consider case: if references are treated case-insensitively anywhere in the app, this index permits `ABC-1` and `abc-1`.

**11. No deploy-ordering note, and these constraints break running app code.** `customer_id NOT NULL` breaks any in-flight insert path that omits it, and the unique index starts raising `23505` on duplicate references. Both need the application deployed to tolerate them *before* the migration runs.

**12. No rollback, and not idempotent.** The index drop is reversible; the `UPDATE` is not — write down the irreversibility explicitly. And re-running after a partial failure fails on the index (`CREATE UNIQUE INDEX` without `IF NOT EXISTS`), which matters because point 5 makes partial failure likely.

## Minor

**13. Index name `uq_orders_ref` doesn't match the column** (`reference`). Prefer `uq_orders_reference`, or just use `ALTER TABLE ... ADD CONSTRAINT ... UNIQUE USING INDEX` so it shows up as a real constraint rather than a bare index.

The shortest path to a safe version: split into three migrations — (a) data remediation with pre-flight checks, batched; (b) `CREATE UNIQUE INDEX CONCURRENTLY`, non-transactional; (c) the `NOT VALID` → `VALIDATE` → `SET NOT NULL` sequence — with `lock_timeout` set in each.

</details>

<details><summary>Last output with the plugin</summary>

Reviewing as PostgreSQL (the lock behavior below is PG-specific; say so if this is MySQL and I'll redo it).

## Problems, most serious first

**1. `SET NOT NULL` does a full table scan holding `ACCESS EXCLUSIVE`.** This is the worst statement in the file — that lock blocks *reads* as well as writes on `orders` for the entire scan. On a large orders table that's a hard outage, not a slowdown. Fix is three steps across separate transactions:

```sql
ALTER TABLE orders ADD CONSTRAINT orders_customer_id_nn
    CHECK (customer_id IS NOT NULL) NOT VALID;   -- instant, no scan
ALTER TABLE orders VALIDATE CONSTRAINT orders_customer_id_nn;  -- SHARE UPDATE EXCLUSIVE, no blocking
ALTER TABLE orders ALTER COLUMN customer_id SET NOT NULL;      -- PG12+: skips the scan
ALTER TABLE orders DROP CONSTRAINT orders_customer_id_nn;
```

**2. No `lock_timeout`.** Any of these three statements can queue behind one long-running query, and a *waiting* `ACCESS EXCLUSIVE` request blocks every subsequent query on the table behind it. That's how a 10ms DDL becomes a 20-minute incident. Add `SET lock_timeout = '3s';` (plus `statement_timeout` on the DDL migrations) so it fails fast and retries instead of stalling the table.

**3. `CREATE UNIQUE INDEX` without `CONCURRENTLY`** takes a `SHARE` lock, blocking all writes to `orders` until the index finishes building. Use `CREATE UNIQUE INDEX CONCURRENTLY`, which means this statement cannot live inside a transaction block — check whether your migration tool wraps migrations in one (Flyway and Liquibase do by default). Then attach it if the app expects a real constraint: `ALTER TABLE orders ADD CONSTRAINT uq_orders_ref UNIQUE USING INDEX uq_orders_ref;`

**4. The `UPDATE` is one unbounded transaction over every NULL-region row.** Long row locks, dead tuples equal to the rows touched, a WAL spike, and replica lag for the duration. Pull it out of the schema migration into a batched, resumable job with keyset pagination:

```sql
WITH batch AS (
    SELECT id FROM orders WHERE id > :last_id AND region IS NULL
    ORDER BY id LIMIT 5000
)
UPDATE orders o SET region = 'EU' FROM batch WHERE o.id = batch.id
RETURNING o.id;
```

**5. The `UPDATE` is irreversible and the migration has no stated rollback.** Once those rows read `'EU'` you cannot distinguish backfilled rows from genuinely-EU rows, so there is no way back. If `region` drives tax, routing, or data residency, mis-defaulting unknown rows to `'EU'` is a compliance problem, not a cosmetic one. Either record the affected ids first, or add a `region_source`/`backfilled_at` marker. Also state the rollback for statements 1–2 explicitly (`DROP NOT NULL`, `DROP INDEX CONCURRENTLY`), or declare the migration forward-only and say why.

**6. `SET NOT NULL` will abort outright if any `customer_id` is NULL** — and there's no pre-check or backfill for it anywhere in this change. Verify `SELECT EXISTS (SELECT 1 FROM orders WHERE customer_id IS NULL)` first; if rows exist, they need a backfill and an app-side fix for whatever is writing NULLs *before* this ships.

**7. `CREATE UNIQUE INDEX` will abort if duplicate `reference` values exist,** and a failed `CONCURRENTLY` build leaves an `INVALID` index behind that you must drop manually before retrying. Check for dupes first, and make the retry path explicit in the runbook.

**8. The unique index doesn't constrain NULL `reference`.** NULLs are distinct in PG, so unlimited NULL references are still permitted. If `reference` is meant to be present and unique, it needs its own `NOT NULL` (via the two-step above), or `NULLS NOT DISTINCT` on PG 15+.

**9. Nothing stops `region` from going NULL again.** The backfill is a one-shot patch with no `DEFAULT` and no `NOT NULL`, so the same NULLs accumulate right after deploy. Decide whether the column should be constrained, and if so do it in the same release plan.

**10. Three statements with three different safe-execution requirements are in one file.** One needs to be outside a transaction, one needs to be split across transactions, one shouldn't be a migration at all. Split into separate migrations plus a backfill job; as written, a failure on statement 2 leaves statement 1 applied with no defined recovery state.

One thing I can't check from the diff: whether this was timed against production-sized data. If the only test was a dev database, the lock durations in items 1 and 3 are unknown rather than small.

</details>
