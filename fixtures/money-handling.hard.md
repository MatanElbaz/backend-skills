```java
public BigDecimal splitEvenly(BigDecimal total, int parts) {
    return total.divide(BigDecimal.valueOf(parts), 2, RoundingMode.HALF_UP);
}

public List<BigDecimal> installments(BigDecimal total, int n) {
    BigDecimal each = splitEvenly(total, n);
    return Collections.nCopies(n, each);
}
```
