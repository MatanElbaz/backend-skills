<!-- result: baseline=3 with=3 runs=3 -->
# Demo: money-handling

Signal (regex, case-insensitive): `RoundingMode|minor units|compareTo|currency`

| Arm | Runs mentioning the signal |
|---|---|
| Without the plugin | 3 / 3 |
| With the plugin | 3 / 3 |

## Code reviewed

```java
public double applyFee(double amount) {
    double fee = amount * 0.0125;
    return Math.round((amount + fee) * 100) / 100.0;
}
```

<details><summary>Last output without the plugin</summary>

Note: I couldn't run the numeric scan to enumerate exact failing inputs — that Bash call wasn't approved — so finding #4 below states the mechanism rather than a specific amount I verified.

## Problems, most serious first

**1. `double` is the wrong type for money.** This is the root cause of most of what follows. `0.0125`, `0.01`, and the returned value itself are not exactly representable in binary floating point. `Math.round(...)/100.0` doesn't give you a value with two decimal places — it gives you the nearest `double` to one, so the "rounded" result is still inexact and will drift the moment it's summed, compared with `==`, or round-tripped through a ledger. Use `BigDecimal` with an explicit scale, or `long` minor units.

**2. No input validation — NaN and infinity return plausible-looking garbage.** `Math.round(double)` returns a `long` and maps `NaN` to `0`, so `applyFee(Double.NaN)` returns `0.0`: a silent, valid-looking "free" transaction. `Double.POSITIVE_INFINITY` maps to `Long.MAX_VALUE`, returning ~`9.22e16`. Both should throw. Negative amounts are also accepted with no statement of whether that's intended (refunds) or a bug.

**3. Rounding mode is unspecified and asymmetric.** `Math.round` is half-up *toward positive infinity*, not half-up away from zero: `Math.round(-2.5) == -2`. So a charge and its matching refund can round in opposite directions and fail to net to zero. Financial code needs an explicitly chosen mode — `HALF_EVEN` (banker's) or `HALF_UP` — not whatever `Math.round` happens to do.

**4. The `*100 → round → /100` idiom misrounds at tie boundaries.** When the true product lands exactly on a half-cent, the `double` nearest to it frequently falls just *below* the boundary, and `Math.round` then rounds down — the classic case being `Math.round(2.675 * 100) == 267`, not `268`. With a rate of 1.25% you hit half-cent ties constantly (any amount ending in `.x4` or `.x8` cents territory), so this is a routine off-by-one-cent, not a corner case.

**5. It rounds the total, not the fee — the fee becomes unauditable.** The caller gets `round(amount + fee)` and has no way to recover what fee was actually charged; subtracting `amount` gives an unrounded, unposted number. Parts won't sum to the total. Most systems need the fee as its own rounded line item for the ledger, invoices, and reconciliation. Return both, and derive the total from the rounded fee.

**6. The rate is a hardcoded magic number.** `0.0125` has no name, no source, no configurability, and no effective-date handling. A rate change becomes a code change and redeploy, and historical transactions can't be recomputed at the rate that applied when they were made. Pass the rate in or resolve it from configuration.

**7. The scale `100` hardcodes a two-decimal currency.** Wrong for JPY (0 decimals — you'd invent fractional yen) and for KWD/BHD/TND (3 decimals — you'd silently truncate). There's no `Currency` parameter, so the method can't be correct for a multi-currency system.

**8. Silent precision loss at large magnitudes.** `(amount + fee) * 100` exceeds 2^53 (~9.0e15) for amounts above roughly 8.9e13, past which not every integer is representable and cent-rounding becomes meaningless. Above ~9.1e16 the `long` saturates at `Long.MAX_VALUE`. No exception either way. Whether those magnitudes are reachable depends on the domain, but nothing here enforces a bound.

**9. Naming and contract.** `applyFee` returns the *total*, not the fee, and "apply" suggests mutation. There's no Javadoc stating the rounding mode, the scale, or the behavior on negatives — so every caller guesses.

## Suggested shape

```java
private static final BigDecimal FEE_RATE = new BigDecimal("0.0125");

/** Returns the fee and total for {@code amount}, each rounded HALF_EVEN to the currency's scale. */
public FeeResult applyFee(BigDecimal amount, Currency currency) {
    if (amount == null || amount.signum() < 0) {
        throw new IllegalArgumentException("amount must be non-negative: " + amount);
    }
    int scale = currency.getDefaultFractionDigits();
    BigDecimal fee = amount.multiply(FEE_RATE).setScale(scale, RoundingMode.HALF_EVEN);
    return new FeeResult(fee, amount.add(fee));
}
```

Rounding the fee and then adding keeps `amount + fee == total` exactly, which the current version does not guarantee. If `BigDecimal` is too heavy for the hot path, `long` cents with an explicit rounding helper is the alternative — but not `double`.

</details>

<details><summary>Last output with the plugin</summary>

Using `backend-skills:money-handling` to review this.

Note: I tried to run a numeric check of the rounding behavior, but both Bash calls needed approval and didn't execute. Everything below is derived from the `Math.round` spec (`(long) Math.floor(a + 0.5d)`) and hand arithmetic, not from a test run.

## Problems, most serious first

**1. `double` for money.** This is the root defect — everything else follows. `0.0125` is 1/80, which has no exact binary representation, so the fee is approximated before any rounding happens. Neither is the returned value: `Math.round(...)/100.0` for a result of 1.15 gives the nearest double to 1.15, not 1.15. Callers that sum these, compare them with `==`, or persist them accumulate error. Use `BigDecimal` with an explicit scale, or `long` minor units.

**2. No currency, and a hardcoded 2-decimal scale.** `* 100` / `/ 100.0` assumes every currency has 2 decimal places. JPY has 0 — this produces fractional yen. KWD, BHD, and TND have 3 — this silently truncates a digit of real value. The scale must come from `Currency.getDefaultFractionDigits()` (rejecting the `-1` it returns for pseudo-currencies like XXX). Because the amount travels without a currency, nothing upstream prevents a USD amount being fed in where EUR was intended.

**3. `Math.round` encodes an undocumented and asymmetric rounding rule.** It is specified as `floor(a + 0.5d)`, i.e. half-up toward positive infinity, not half-up by magnitude. So `Math.round(-2.5) == -2`. Any negative amount — refund, chargeback, reversal, credit note — rounds in the opposite direction from the positive case, so a charge and its reversal don't cancel. The business rule is never named; pick a `RoundingMode` explicitly and document why.

**4. Same call silently produces garbage for non-finite and large inputs.** `Math.round(NaN)` is `0`, so a NaN amount becomes a fee-applied total of `0.0`. `Math.round(+Infinity)` is `Long.MAX_VALUE`, and anything beyond `long` range clamps rather than throwing — so an absurd amount yields a plausible-looking number instead of an error. Separately, there's a precision cliff well below overflow: once `(amount + fee) * 100` exceeds 2^53 (amounts above roughly 9×10¹³), the scaled value can't represent consecutive integers, so the "round to cents" step stops being meaningful.

**5. Exact half-cent ties are resolved by binary noise, not by your rounding rule.** `amount × 1.0125` lands on an exact half-cent whenever amount is 0.40, 1.20, 2.00, 2.80, … (every 0.80). These are precisely the cases where the rounding mode is supposed to decide, and here the decision is made instead by whether the accumulated double error happens to land above or below the tie. The outcome is deterministic but arbitrary, and it changes if you reorder the arithmetic.

**6. The fee is computed and then thrown away.** Only the combined total is returned, already rounded. The caller cannot recover the fee as a bookkeeping line, and `principal + fee == total` won't hold, since the fee was never rounded to a representable cent amount of its own. Ledger lines won't reconcile against the charged total. Return both components, each rounded with the same named mode, and make the remainder assignment explicit.

**7. The fee rate is a hardcoded literal with no provenance.** `0.0125` can't be configured, can't be versioned, and isn't recorded on the transaction. When the rate changes you lose the ability to reprice or audit historical transactions, because nothing says which rate was in effect. Pass it in, and persist it alongside the amount.

**8. `double` in the signature propagates the problem past this function.** A `double` return gets serialized as a JSON number that clients may parse as a float, and tends to land in a `float`/`double precision` column. Amounts should cross boundaries as strings or integer minor units, and be stored as `numeric(p,s)` or `bigint`.

**9. No input validation.** Negative, NaN, and infinite amounts are all accepted. Whether a negative amount should even reach a fee calculation is a business question this code answers by accident.

---

A shape that addresses the above:

```java
/** Returns the fee and the gross total as separate, exact amounts. */
public record FeeResult(BigDecimal fee, BigDecimal total) {}

public FeeResult applyFee(BigDecimal amount, Currency currency, BigDecimal feeRate) {
    int scale = currency.getDefaultFractionDigits();
    if (scale < 0) throw new IllegalArgumentException("not a real currency: " + currency);

    // Rounding rule: HALF_EVEN. Replace with whatever the contract or jurisdiction requires.
    BigDecimal fee = amount.multiply(feeRate).setScale(scale, RoundingMode.HALF_EVEN);
    return new FeeResult(fee, amount.add(fee));   // total is exact: both operands at `scale`
}
```

Rounding the fee once, to the currency's own scale, keeps `amount + fee == total` exactly, so the two ledger lines always sum to what you charged.

</details>
