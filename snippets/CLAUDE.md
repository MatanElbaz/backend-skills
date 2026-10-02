# Backend rules

Copy this into a project's `CLAUDE.md` or `AGENTS.md`. Each rule links to a fuller skill in the plugin.

## idempotency
- Anything that can be retried or redelivered needs an idempotency key from the caller or the message ID.
- Enforce it with a unique constraint, inserted in the same transaction as the side effect. Reject the same key with a different payload.

## transaction-boundaries
- No network calls inside a database transaction.
- Publish events through a transactional outbox, never before commit and never as a bare "after commit" call.

## timeouts-and-retries
- Every outbound call has explicit timeouts. Retry only transient failures on idempotent operations, with exponential backoff, jitter, and a cap.
- Retry at one layer only.

## safe-migrations
- Set `lock_timeout`. Build indexes `CONCURRENTLY`. Add constraints `NOT VALID`, then validate.
- Backfill in batches, outside the schema migration. Expand and contract, never rename in place.

## money-handling
- No `float` or `double` for money. Amounts carry a currency, every rounding names its `RoundingMode`, and equality uses `compareTo`.
