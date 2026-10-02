---
name: money-handling
description: Use when code stores, computes, converts, compares, or displays monetary amounts or currencies
---

# Money handling

Money bugs are quiet. They show up as a cent off per thousand transactions, found months later by reconciliation.

## When to use

- Any field, column, or calculation that represents an amount.
- Fees, taxes, discounts, splits, currency conversion, or comparing amounts.

## Checklist

1. Never `float` or `double`. Use integer minor units (`long` cents) or `BigDecimal` with an explicit scale.
2. An amount always travels with its currency. Never add or compare across currencies.
3. The number of decimal places depends on the currency (JPY has 0, KWD has 3). Take it from ISO 4217 data, for example `Currency.getDefaultFractionDigits()`, never from a hardcoded 2.
4. Every division or scale change names its `RoundingMode`. `BigDecimal.divide` without one throws on non-terminating results. Document the business rule you chose.
5. `BigDecimal.equals` compares scale, so `2.0` does not equal `2.00`. Compare with `compareTo`.
6. When splitting an amount (shares, installments), the parts must sum to the total. Distribute the remainder explicitly instead of rounding each part.
7. In JSON, send amounts as strings or as integer minor units, not as floating-point numbers. In the database use `numeric(p, s)` or `bigint`, never `float` or `double precision`.
8. For conversion, record the rate, its timestamp, and its source, and convert once at a defined point.

## Bad example

```java
double total = 0;
for (Line l : lines) {
    total += l.getPrice() * l.getQty() * 1.17;   // price + VAT
}
if (total == invoice.getAmount()) {
    markPaid();
}
return total;                                     // serialized as a JSON number
```

## What to flag

- `double` for money: `0.1 + 0.2` is not `0.3`, and errors accumulate across lines.
- `==` on floating-point amounts will miss equal values and may match different ones.
- No currency anywhere, so nothing stops a USD amount being compared with an EUR one.
- VAT applied per line with no rounding rule: the total can differ from the invoice by a cent.
- The result is serialized as a JSON number, which clients may parse as a float.

## Good example

```java
Currency currency = invoice.currency();
BigDecimal vat = new BigDecimal("1.17");

BigDecimal net = lines.stream()
    .map(l -> l.unitPrice().multiply(BigDecimal.valueOf(l.qty())))
    .reduce(BigDecimal.ZERO, BigDecimal::add);

BigDecimal gross = net.multiply(vat)
    .setScale(currency.getDefaultFractionDigits(), RoundingMode.HALF_EVEN);

if (gross.compareTo(invoice.amount()) == 0) {
    markPaid();
}
return gross.toPlainString();                     // string, not a float
```

Rounding once, at the end, with a named mode, keeps the total predictable. If the business rule is to round per line, say so in the code and round each line with the same mode.
