```sql
-- V42__add_status.sql  (Flyway, runs in a single transaction)
ALTER TABLE payments ADD COLUMN status text;
UPDATE payments SET status = 'NEW';
ALTER TABLE payments ALTER COLUMN status SET NOT NULL;
ALTER TABLE payments ADD CONSTRAINT payments_status_chk CHECK (status IN ('NEW', 'DONE'));
CREATE INDEX CONCURRENTLY idx_payments_status ON payments (status);
```
