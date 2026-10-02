```java
public Quote fetchQuote(String symbol) {
    for (int i = 0; i < 5; i++) {
        try {
            return restTemplate.getForObject(url + "/quote/" + symbol, Quote.class);
        } catch (RestClientException e) {
            log.warn("retrying", e);
        }
    }
    throw new IllegalStateException("quote unavailable");
}
```
