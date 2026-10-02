```sql
ALTER TABLE orders ALTER COLUMN customer_id SET NOT NULL;
CREATE UNIQUE INDEX uq_orders_ref ON orders (reference);
UPDATE orders SET region = 'EU' WHERE region IS NULL;
```
