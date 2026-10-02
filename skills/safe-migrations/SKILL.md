---
name: safe-migrations
description: Use when writing or reviewing a database schema migration, backfill, or data fix, especially on a table that is large or in constant use
---

# Safe migrations

Most migration outages are not wrong SQL. They are correct SQL that takes a lock the application cannot live without, or that runs for an hour. This skill assumes PostgreSQL. Check your version where noted.

## When to use

- Any `ALTER TABLE`, `CREATE INDEX`, `UPDATE`, or `DELETE` shipped as a migration.
- Adding a constraint, changing a type, renaming or dropping a column.

## Checklist

1. Set `lock_timeout` (and usually `statement_timeout`) in the migration, so it fails fast instead of queueing behind a long query and blocking everything behind it.
2. Build indexes with `CREATE INDEX CONCURRENTLY`. It cannot run inside a transaction block, so the migration tool must run it outside one.
3. Add constraints in two steps: add `NOT VALID` first, then `VALIDATE CONSTRAINT`. Validation does not block writes.
4. `SET NOT NULL` scans the whole table under a strong lock. On PostgreSQL 12 and later it skips the scan if a validated `CHECK (col IS NOT NULL)` already exists, so add that first.
5. Use expand and contract. Add the new column, deploy code that writes both, backfill, switch reads, and drop the old column in a later release. Never rename or drop in the same release that stops using the column.
6. Backfill in small batches with keyset pagination, throttled and resumable, outside the schema migration. One `UPDATE` over a whole table is one huge transaction.
7. State the rollback. If the migration is forward-only, say so and say why.
8. Time it against production-sized data, not a dev database with 200 rows.

## Bad example

```sql
CREATE INDEX idx_payments_status ON payments (status);
UPDATE payments SET status = 'DONE' WHERE settled_at IS NOT NULL;
ALTER TABLE payments ALTER COLUMN reference SET NOT NULL;
```

## What to flag

- `CREATE INDEX` without `CONCURRENTLY` blocks all writes to `payments` while it builds.
- The `UPDATE` rewrites every matching row in one transaction: long locks, table bloat, replication lag.
- `SET NOT NULL` does a full scan under an exclusive lock.
- No `lock_timeout`, so any of these can queue behind a long-running query and stall the whole table.

## Good example

```sql
SET lock_timeout = '3s';

-- outside a transaction block
CREATE INDEX CONCURRENTLY idx_payments_status ON payments (status);

ALTER TABLE payments
    ADD CONSTRAINT payments_reference_nn CHECK (reference IS NOT NULL) NOT VALID;
ALTER TABLE payments VALIDATE CONSTRAINT payments_reference_nn;
ALTER TABLE payments ALTER COLUMN reference SET NOT NULL;   -- PostgreSQL 12+: no full scan
ALTER TABLE payments DROP CONSTRAINT payments_reference_nn;
```

Backfill as a separate, repeatable job:

```sql
UPDATE payments SET status = 'DONE'
WHERE id IN (
    SELECT id FROM payments
    WHERE id > :last_id AND settled_at IS NOT NULL AND status IS DISTINCT FROM 'DONE'
    ORDER BY id
    LIMIT 5000
);
```

Run it in a loop, advancing `:last_id` to the largest `id` processed, with a pause between batches. It is safe to stop and restart.
