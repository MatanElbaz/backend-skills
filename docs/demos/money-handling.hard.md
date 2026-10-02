<!-- result: baseline=3 with=3 runs=3 -->
# Demo: money-handling.hard

Signal (regex, case-insensitive): `remainder|do(es)? not (add|sum)|99\.99|allocat|distribut(e|ing) the (cent|rest|difference)|sum(s)? (to|up to|equal)`

| Arm | Runs mentioning the signal |
|---|---|
| Without the plugin | 3 / 3 |
| With the plugin | 3 / 3 |

## Code reviewed

```java
public BigDecimal splitEvenly(BigDecimal total, int parts) {
    return total.divide(BigDecimal.valueOf(parts), 2, RoundingMode.HALF_UP);
}

public List<BigDecimal> installments(BigDecimal total, int n) {
    BigDecimal each = splitEvenly(total, n);
    return Collections.nCopies(n, each);
}
```

<details><summary>Last output without the plugin</summary>

## 1. Installments don't sum to the total — money is created or destroyed

This is the defect that matters. `installments(new BigDecimal("100.00"), 3)` returns `[33.33, 33.33, 33.33]` = **99.99**. One cent vanishes. In the other direction, `installments(new BigDecimal("0.05"), 2)` returns `[0.03, 0.03]` = **0.06** — HALF_UP invents a cent.

For a payment schedule this means the sum of charges ≠ the amount authorized, which shows up as reconciliation breaks, a dangling balance that never closes, or an over-collection. The remainder has to be allocated explicitly, not rounded away.

## 2. `parts <= 0` is unguarded

`parts == 0` throws a raw `ArithmeticException: Division by zero` from inside `divide`; negative `n` gets past `splitEvenly` and dies later in `Collections.nCopies` with `IllegalArgumentException`. Two different exceptions, neither naming the actual problem. `total == null` NPEs. Validate at the boundary with a message that identifies the bad input.

## 3. Scale 2 is hardcoded, so this is wrong for non-2-decimal currencies

JPY and KRW have no minor unit; BHD, KWD, and TND have three. Splitting ¥100 into 3 yields `33.33`, which isn't a representable amount. Scale must come from the currency (`Currency.getDefaultFractionDigits()`), which is really an argument that the signature should take a money type rather than a bare `BigDecimal`.

## 4. `Collections.nCopies` returns an immutable list

Correctness is fine — `BigDecimal` is immutable, so the shared reference is harmless — but the caller gets a fixed-size, unmodifiable view. Any caller trying to adjust one entry (exactly what fixing problem #1 requires downstream) gets `UnsupportedOperationException`. Return a real `ArrayList`.

## 5. HALF_UP is probably the wrong default, and is wrong for negatives

HALF_UP rounds away from zero, so a `-0.025` refund line becomes `-0.03`. Accounting conventions generally want HALF_EVEN for aggregate figures to avoid systematic upward bias across many transactions. Whichever you pick should be a deliberate, documented choice rather than the first mode that came to hand.

## 6. `total`'s incoming scale is silently discarded

If `total` arrives as `100.005` (common from a tax or FX calculation), `divide` quietly folds that rounding into the per-installment figure with no record that it happened. Normalize `total` to currency scale explicitly first, so the rounding is one visible step.

## 7. `splitEvenly` is a misleading public name

It doesn't split anything — it divides and rounds, and it's the exact primitive that must not be used for money. As a `public` method it's an attractive nuisance; either make it private or name it for what it is. (Minor: the two methods call the same parameter `parts` and `n`.)

---

A version that addresses 1–6:

```java
public List<BigDecimal> installments(BigDecimal total, int n, int scale) {
    if (total == null) throw new IllegalArgumentException("total is required");
    if (n <= 0) throw new IllegalArgumentException("n must be > 0, was " + n);

    BigDecimal unit   = BigDecimal.ONE.movePointLeft(scale);              // 0.01
    BigDecimal amount = total.setScale(scale, RoundingMode.HALF_EVEN);    // explicit normalization
    BigDecimal base   = amount.divide(BigDecimal.valueOf(n), scale, RoundingMode.DOWN);

    long extra = amount.subtract(base.multiply(BigDecimal.valueOf(n)))
                       .divide(unit).longValueExact();                    // minor units left over, |extra| < n
    BigDecimal bump = extra < 0 ? base.subtract(unit) : base.add(unit);
    long bumped = Math.abs(extra);

    List<BigDecimal> out = new ArrayList<>(n);
    for (int i = 0; i < n; i++) out.add(i < bumped ? bump : base);
    return out;
}
```

`100.00 / 3` → `[33.34, 33.33, 33.33]`, summing to exactly `100.00`. Two things to settle as product decisions: whether the extra cents land on the **first** or **last** installments (front-loading is the usual choice, and either way the allocation must be deterministic so regenerating a schedule reproduces the same amounts), and whether callers should be handed a currency-aware money type instead of `BigDecimal` + `scale`. Worth a test asserting `sum(installments(t, n)) == t` over a range of totals and `n`, including negatives and `n` values that don't divide evenly.

</details>

<details><summary>Last output with the plugin</summary>

Using `backend-skills:money-handling` as the review rubric.

## Problems, most serious first

**1. The installments don't sum to the total.** This is the actual bug. `splitEvenly` rounds the per-part amount, then `nCopies` multiplies that rounded value back up, so the rounding error is multiplied by `n` instead of absorbed.

- `installments(100.00, 3)` → 3 × `33.33` = **99.99** — you under-collect a cent on every such plan.
- `installments(100.00, 6)` → 6 × `16.67` = **100.02** — worse, you over-collect. Charging a customer more than the total is a chargeback/compliance problem, not a reconciliation nit.

A split must distribute the remainder explicitly: truncate each part, then hand out the leftover minor units one at a time.

**2. Hardcoded scale of 2, and no currency anywhere.** Both methods take a bare `BigDecimal`, so nothing says what currency this is, and `2` is wrong for a large share of ISO 4217: JPY/KRW have 0 decimals (you'd invent sub-yen amounts that can't be charged), KWD/BHD have 3 (you'd silently round away a third decimal of real money). Scale must come from `Currency.getDefaultFractionDigits()`, with pseudo-currencies (`-1`, e.g. XXX) rejected.

**3. `parts`/`n` is unvalidated and fails in two different ugly ways.** `n == 0` throws `ArithmeticException: Division by zero` from `divide`. `n < 0` is worse: the division *succeeds* and returns a negative per-part amount, then `Collections.nCopies` throws `IllegalArgumentException` — so the failure surfaces from the wrong line with a message that says nothing about the real problem. Guard `n <= 0` up front.

**4. `HALF_UP` is asserted, not justified.** The skill's rule is that every rounding mode names its business rule. For installment schedules, HALF_UP on each part is essentially never the correct rule — the convention is truncate-and-distribute (remainder to the first or last installment, per contract). If HALF_UP really is the contractual rule here, it needs a comment saying so and whose rule it is.

**5. `total` with a finer scale than the currency is silently truncated.** `splitEvenly(100.005, 2)` quietly becomes `50.00` per part — a sub-cent input disappears with no signal. Decide explicitly: either reject inputs finer than the currency's scale, or round once at a named boundary and document it.

**6. `Collections.nCopies` returns an immutable, lazily-materialized view.** Even after the math is fixed, the remainder has to land somewhere, which means callers need a list whose elements can differ. Returning an immutable list here also means any caller that tries `list.set(n-1, adjusted)` gets `UnsupportedOperationException` at runtime rather than a compile-time nudge. Return a real `ArrayList`.

**7. Scale-sensitive equality is a trap you're handing to callers.** Every value coming out of here has scale 2 (or 3, after the fix), so a caller comparing against a `BigDecimal` parsed from elsewhere with `equals` will get false for equal amounts. Nothing in this code forces that bug, but if callers compare these values, they must use `compareTo`.

## Suggested shape

```java
/**
 * Splits total into n installments that sum exactly to total.
 * Rounding rule: each installment is truncated toward zero and the leftover
 * minor units are added to the earliest installments (front-loaded).
 */
public List<BigDecimal> installments(BigDecimal total, Currency currency, int n) {
    if (n <= 0) throw new IllegalArgumentException("installments must be positive: " + n);
    int scale = currency.getDefaultFractionDigits();
    if (scale < 0) throw new IllegalArgumentException("currency has no minor unit: " + currency);

    BigDecimal exact = total.setScale(scale);                      // throws if total is finer than the currency
    BigDecimal count = BigDecimal.valueOf(n);
    BigDecimal each = exact.divide(count, scale, RoundingMode.DOWN);
    BigDecimal minorUnit = BigDecimal.ONE.movePointLeft(scale);    // 0.01 for USD, 1 for JPY
    int extra = exact.subtract(each.multiply(count)).divide(minorUnit).intValueExact();
    BigDecimal step = extra < 0 ? minorUnit.negate() : minorUnit;  // refunds split too

    List<BigDecimal> out = new ArrayList<>(n);
    for (int i = 0; i < n; i++) {
        out.add(i < Math.abs(extra) ? each.add(step) : each);
    }
    return out;
}
```

`installments(100.00, USD, 6)` → four × `16.67` + two × `16.66` = `100.00`. `installments(-100.00, USD, 6)` → sums to `-100.00`.

The invariant worth writing as a property test: for any total, currency, and `n > 0`, the parts sum to the total and no two parts differ by more than one minor unit. `splitEvenly` itself is fine as a standalone "what's the even share" helper once it takes a currency — it just must not be the basis for a split that has to add up.

</details>
