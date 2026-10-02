# Backend rules

Copy this into a project's `CLAUDE.md` or `AGENTS.md`. Each section matches the skill of the same name in https://github.com/MatanElbaz/backend-skills, which has the full checklist and examples.

## idempotency
- Anything that can be retried or redelivered needs an idempotency key from the caller or the message ID.
- Claim the key with a unique constraint before doing any work. If the side effect is only database writes, do it in the same transaction. If the work calls other systems, claim it as `IN_PROGRESS` with a lease in its own short transaction, do the work outside any transaction, then mark it `DONE`.
- Reject the same key with a different payload (409 or 422), and let a retry take over an expired lease.

## transaction-boundaries
- No network calls inside a database transaction.
- Publish events through a transactional outbox, never before commit and never as a bare "after commit" call.

## timeouts-and-retries
- Every outbound call has explicit timeouts. Retry only transient failures on operations that are idempotent or carry an idempotency key, with exponential backoff, jitter, and a cap.
- Retry at one layer only.

## safe-migrations
- Set `lock_timeout`. Build indexes `CONCURRENTLY`. Add CHECK and foreign-key constraints `NOT VALID`, then validate in a separate transaction.
- Backfill in batches, outside the schema migration. Expand and contract, never rename in place.

## money-handling
- No `float` or `double` for money. Amounts carry a currency, every rounding names its `RoundingMode`, and equality uses `compareTo`.
