```java
public double applyFee(double amount) {
    double fee = amount * 0.0125;
    return Math.round((amount + fee) * 100) / 100.0;
}
```
