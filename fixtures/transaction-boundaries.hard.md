```java
@Service
public class RefundService {

    @Transactional
    public void refundAll(List<Long> orderIds) {
        for (Long id : orderIds) {
            refundOne(id);
        }
    }

    @Transactional(propagation = Propagation.REQUIRES_NEW)
    public void refundOne(Long id) throws PaymentException {
        Order o = orders.findById(id).orElseThrow();
        o.setStatus(OrderStatus.REFUNDED);
        gateway.refund(o.getPaymentId());   // throws checked PaymentException
    }
}
```
