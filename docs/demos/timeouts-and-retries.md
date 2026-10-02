<!-- result: baseline=3 with=3 runs=3 -->
# Demo: timeouts-and-retries

Signal (regex, case-insensitive): `jitter|retry storm|thundering herd|connect(ion)? timeout|4xx`

| Arm | Runs mentioning the signal |
|---|---|
| Without the plugin | 3 / 3 |
| With the plugin | 3 / 3 |

## Code reviewed

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

<details><summary>Last output without the plugin</summary>

Reviewing inline — no files to inspect.

## Problems, most serious first

**1. Retries non-retryable failures, with no backoff — this is a retry amplification bug.**
`RestClientException` is the root of the hierarchy, so a 400 (malformed symbol), 401/403 (bad credentials), and 404 (unknown symbol) all get retried 5 times in a tight loop. Those are deterministic — the 2nd through 5th attempts cannot succeed. And when the upstream *is* genuinely degraded (503/504), every caller instantly multiplies its load by 5× with zero delay. That is the classic pattern that turns an upstream brownout into an outage. Retry only on `ResourceAccessException` (I/O, connect/read timeout) and 5xx/429, and add exponential backoff with jitter.

**2. No circuit breaker or overall deadline — threads will be exhausted during an upstream outage.**
This is synchronous and blocking, presumably on a request-serving thread. During an upstream failure every call burns `5 × readTimeout` before failing. With a 30s read timeout that's 150s per request, the servlet thread pool saturates, and the failure cascades into endpoints that have nothing to do with quotes. Needs a Resilience4j circuit breaker (or equivalent) plus a total time budget checked between attempts, not just an attempt count.

**3. The root cause is thrown away.**
`throw new IllegalStateException("quote unavailable")` has no cause. The one exception a caller actually sees contains no information about *why* — connection refused vs. 401 vs. timeout are indistinguishable in production. Capture the last exception and pass it as the cause. Relatedly, `log.warn("retrying", e)` logs no symbol, no attempt number, no URL, and no status code, so the five identical WARN lines are nearly useless for triage.

**4. `getForObject` can return `null` and that null is returned as success.**
On a 204 or an empty body, `RestTemplate#getForObject` returns `null`. The method hands that back as a successful `Quote`, and the caller NPEs somewhere far away from this code with no indication of the origin. Decide explicitly: treat an empty body as a failure (retry or throw), or return `Optional<Quote>`.

**5. Timeout safety is implicit, not guaranteed.**
Everything above assumes the injected `RestTemplate` has connect and read timeouts set. With the default `SimpleClientHttpRequestFactory` and no explicit configuration, both are infinite — a single hung socket parks the thread permanently and the retry loop never even gets to iterate. Worth confirming in the `RestTemplate` bean config; if it isn't set there, this method is unbounded.

**6. `symbol` is concatenated into the URL unencoded.**
`url + "/quote/" + symbol` with a symbol containing `/`, `?`, `#`, or a space produces a malformed or entirely different request path — `../internal/keys` style traversal against the upstream is reachable if `symbol` comes from user input. Use the URI-variable overload, which encodes: `getForObject(url + "/quote/{symbol}", Quote.class, symbol)`. Validate the symbol against an expected pattern as well.

**7. `IllegalStateException` is the wrong type.**
It means "this object is in an illegal state," which is not what happened — a dependency failed. Callers cannot distinguish "no such symbol" from "quote service is down," so they can't choose between a 404 and a 503, and most exception handlers map `IllegalStateException` to a 500. Use a dedicated exception (e.g. `QuoteUnavailableException`) and separate the not-found case from the upstream-failure case.

**8. Hardcoded attempt count, no observability.**
`5` is a magic number buried in a loop; it should be configuration. There are also no metrics on retry attempts or on retry exhaustion, which are exactly the two signals you need to detect that the upstream is degrading before customers report it.

One structural note: items 1, 2, and 8 are all arguing that this hand-rolled loop should be declarative config on a Resilience4j (or Spring Retry) decorator instead of imperative code — that gets you the retry predicate, backoff, jitter, circuit breaker, and metrics in one place, and leaves this method with just the call.

</details>

<details><summary>Last output with the plugin</summary>

Using `backend-skills:timeouts-and-retries` to review against the outbound-call checklist.

## Problems, most serious first

**1. No timeout visible on the `RestTemplate` — the retry loop can pin a thread indefinitely.**
If this `RestTemplate` was built with the default `SimpleClientHttpRequestFactory` and no explicit connect/read timeouts, both are infinite. A hung TCP connection never throws, so `RestClientException` never fires, the loop never advances, and the request thread is held forever. Under a dependency blackhole that exhausts the Tomcat worker pool and takes down endpoints unrelated to quotes. Verify the bean config; if timeouts aren't set there, this is the bug that matters. (With timeouts set, 5 attempts still has no *total* bound — see #5.)

**2. Retries every `RestClientException`, including failures that can never succeed.**
`RestClientException` is the root of the hierarchy. It covers `HttpClientErrorException` (400/401/403/404), `HttpMessageNotReadableException` (response body doesn't match `Quote` — a schema bug), and `UnknownContentTypeException`. An unknown symbol returning 404 costs 5 upstream calls and 5 WARN logs before failing. A deserialization bug retries 5× on every single request. Retry only on `ResourceAccessException` (timeouts, connection resets) and on 503/502/504/429/408.

**3. No delay between attempts, and no jitter.**
Five back-to-back calls with zero backoff hammer a dependency that is already failing — this is retry amplification, 5× load precisely when upstream is least able to serve it. And because every instance retries immediately, all of them retry in lockstep. Needs exponential backoff with jitter.

**4. `symbol` is concatenated into the URL path unencoded.**
`url + "/quote/" + symbol` with a symbol like `../admin` or `BRK.B?foo=` produces a path you didn't intend. Use the URI-template form so RestTemplate encodes it: `restTemplate.getForObject(url + "/quote/{symbol}", Quote.class, symbol)`.

**5. No total time budget, so worst-case latency is unbounded from the caller's view.**
Even with per-call timeouts, 5 × (connect + read) + backoff must fit inside the deadline of whoever is calling `fetchQuote`. Nothing here caps it or checks remaining budget. Pick the attempt count and timeouts from the upstream deadline, not from a magic `5`.

**6. The final exception discards the cause.**
`throw new IllegalStateException("quote unavailable")` drops `e` entirely. On-call sees "quote unavailable" with no indication whether it was a connect timeout, a 503, or a JSON mismatch. Capture the last exception and pass it as the cause.

**7. `IllegalStateException` is the wrong type and erases the distinction callers need.**
"Upstream is down" (→ 503/504 to your client, retryable) and "no such symbol" (→ 404, permanent) collapse into one generic unchecked exception. Define a `QuoteUnavailableException` / `QuoteNotFoundException` pair so the web layer can map status codes and so your own alerting doesn't page on bad user input.

**8. `getForObject` can return `null`.**
A 204 or empty body yields `null`, which this method returns as a successful `Quote`, pushing an NPE into the caller. Treat null as a failed attempt or reject it explicitly.

**9. No circuit breaker, and `Retry-After` is ignored.**
During a sustained outage every request pays the full 5-attempt cost before failing. A breaker fails fast and lets the dependency recover. On 429/503 the upstream is telling you when to come back — honor the header instead of your own schedule.

**10. Check that nothing else is retrying.**
If the `RestTemplate` has a retry interceptor, or a service mesh / API gateway sits in front, 5 here multiplies with those. Retry at one layer only.

**11. Log line carries no context.**
`log.warn("retrying", e)` omits the symbol, the attempt number, and the status. Five identical lines per failure, unjoinable to a request. At WARN × 5 per request this is also a log-volume problem during an outage.

## Shape of the fix

```java
private static final RetryConfig CONFIG = RetryConfig.custom()
    .maxAttempts(3)
    .intervalFunction(IntervalFunction.ofExponentialRandomBackoff(100, 2.0, 0.5))
    .retryOnException(e ->
        e instanceof ResourceAccessException
        || (e instanceof HttpServerErrorException)
        || (e instanceof HttpClientErrorException.TooManyRequests))
    .build();

// RestTemplate built with explicit connect + read timeouts, e.g. 500ms / 2s.

public Quote fetchQuote(String symbol) {
    try {
        return Retry.of("quote", CONFIG).executeCheckedSupplier(() -> {
            Quote q = restTemplate.getForObject(url + "/quote/{symbol}", Quote.class, symbol);
            if (q == null) throw new QuoteUnavailableException("empty body for " + symbol, null);
            return q;
        });
    } catch (HttpClientErrorException.NotFound e) {
        throw new QuoteNotFoundException(symbol, e);
    } catch (Throwable t) {
        throw new QuoteUnavailableException("quote unavailable for " + symbol, t);
    }
}
```

Worst case: 3 × (500ms + 2s) + <0.5s backoff ≈ under 8s — and that figure has to fit inside the deadline of whatever calls this. Note `HttpRequest`-style read timeouts cap time to response headers, not a slow body read; if quote payloads can be large, bound that separately.

Two things I couldn't verify from the snippet: the `RestTemplate` bean's timeout configuration (#1) and whether another layer already retries (#10). Both change the severity ranking, so worth confirming before you touch the loop.

</details>
