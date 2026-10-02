```java
@Component
public class OrderClient {
    @Retryable(maxAttempts = 3, backoff = @Backoff(delay = 200))
    public Order fetch(String id) {
        return http.getForObject("/orders/" + id, Order.class);
    }
}

@Service
public class SummaryService {
    @Retryable(maxAttempts = 3, backoff = @Backoff(delay = 200))
    public Summary summarize(String id) {
        return Summary.of(orderClient.fetch(id));
    }
}
```
