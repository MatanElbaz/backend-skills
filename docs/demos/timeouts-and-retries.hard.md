<!-- result: baseline=3 with=3 runs=3 -->
# Demo: timeouts-and-retries.hard

Signal (regex, case-insensitive): `multipl|amplif|nested retr|retries? (at|in) (one|a single) (layer|level)|each layer|9 (calls|attempts|requests)|jitter`

| Arm | Runs mentioning the signal |
|---|---|
| Without the plugin | 3 / 3 |
| With the plugin | 3 / 3 |

## Code reviewed

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

<details><summary>Last output without the plugin</summary>

No skill tooling is available in this session, so here's the review directly.

## Review: nested `@Retryable` in `OrderClient` / `SummaryService`

**1. Retry amplification — 3 × 3 = 9 downstream calls per logical request.**
`summarize` retries, and each of its attempts calls `fetch`, which retries internally. One user request can fire 9 HTTP calls at `/orders/{id}`. Under partial outage this multiplies load on an already-struggling dependency — the classic retry storm that converts a brownout into an outage. Retry belongs at exactly one layer. Pick the client (`OrderClient`) and drop `@Retryable` from `SummaryService`.

**2. No request timeouts shown — retry without a timeout is the dangerous combination.**
If the downstream hangs rather than fails fast, each of the 9 attempts blocks on socket read indefinitely. Retry adds nothing and holds the caller's thread (likely a Tomcat worker) for the duration. Set connect + read timeouts on the `RestTemplate`'s request factory before anything else; a retry policy is only meaningful once failure is bounded in time.

**3. Retries non-retryable failures.**
No `retryFor`/`noRetryFor`, so the default is "retry on any `Exception`". `getForObject` throws `HttpClientErrorException` for 400/401/403/404 — all deterministic. Retrying a 404 three times (nine, with the nesting) is pure waste and pollutes error-rate metrics. Restrict to transient classes:

```java
@Retryable(
    retryFor = {ResourceAccessException.class, HttpServerErrorException.class},
    noRetryFor = HttpClientErrorException.class,
    maxAttemptsExpression = "${orders.retry.maxAttempts:3}",
    backoff = @Backoff(delayExpression = "${orders.retry.delay:200}", multiplier = 2, maxDelay = 2000, random = true))
```

**4. `@EnableRetry` is not visible anywhere.**
Without it on a `@Configuration` class, Spring Retry registers no advisor and both annotations are silent no-ops — the code looks resilient and isn't. Worth confirming, and worth an integration test that asserts N invocations on a stubbed server, because this failure mode is invisible at runtime.

**5. Fixed 200 ms delay, no multiplier or jitter → thundering herd.**
Every concurrent caller that fails at T retries at T+200, T+400, T+600 in lockstep. Add `multiplier` and `random = true` (included above).

**6. NPE path that is itself retried.**
`getForObject` returns `null` on a 204 or empty body, so `Summary.of(null)` likely throws NPE — which the default retry policy treats as retryable, producing three identical failures for a deterministic bug. Handle the absent-order case explicitly (`Optional`, or a domain `OrderNotFoundException`) rather than letting `null` flow into the mapper.

**7. Unencoded path concatenation.**
`"/orders/" + id` breaks on any `id` containing `/`, `?`, `#`, or whitespace, and allows extra path segments to be injected into the URI. Use the template form so `RestTemplate` encodes it: `http.getForObject("/orders/{id}", Order.class, id)`.

**8. No `@Recover` and no circuit breaker.**
On exhaustion the raw `HttpServerErrorException` (or `ExhaustedRetryException`) escapes to the caller — no degraded summary, no domain-level error type. And with no breaker, the service keeps retrying into a dependency that is known-down, instead of failing fast and shedding load. Retry handles blips; a breaker handles sustained failure. You need both or neither is sufficient.

**9. Retry on a business-layer method is a latent correctness bug.**
`summarize` is retry-safe today only because it happens to be a single idempotent GET plus a pure mapping. The moment it gains a side effect — a write, an event publish, a metric increment — `@Retryable` silently duplicates it. Keeping retry in the client (point 1) also fixes this structurally.

**10. No observability on retries.**
No `RetryListener`, no metrics, no log on attempt. Retries are the main driver of unexplained p99 latency; today they'd be invisible. Register a listener that increments a counter tagged by attempt number and outcome.

**11. Hardcoded policy constants.**
`maxAttempts` and `delay` are compile-time literals, so they can't be tuned per environment or turned off during an incident. Use the `*Expression` attributes bound to properties (shown in point 3).

**Priority if you only fix three:** remove the inner-or-outer duplicate retry (1), add HTTP timeouts (2), and scope retries to transient exceptions (3).

</details>

<details><summary>Last output with the plugin</summary>

Using `backend-skills:timeouts-and-retries` to review against the retry/timeout checklist.

## Problems, most serious first

**1. No timeout anywhere on the HTTP call — the retry config is dead weight**
`RestTemplate`'s default `SimpleClientHttpRequestFactory` has no connect or read timeout. A hung connection (SYN accepted, no response bytes) never throws, so the thread blocks indefinitely and `@Retryable` never fires. The failure mode this code is meant to protect against is the one it can't handle. Set connect + read timeouts on the builder, e.g.:

```java
RestTemplateBuilder b = builder
    .setConnectTimeout(Duration.ofMillis(500))
    .setReadTimeout(Duration.ofSeconds(2));
```
Note a read timeout caps the gap between bytes, not total body time — add a response-size/total cap if order payloads can be large.

**2. Retry at two layers — 3 × 3 = 9 requests per `summarize()` call**
`summarize` retries, and the `fetch` it calls retries internally. A struggling orders service gets 9× the normal load from every caller at exactly the moment it is failing. Pick one layer — keep `@Retryable` on `OrderClient.fetch` (closest to the transport, where it knows what's transient) and remove it from `SummaryService`.

**3. Retries every exception, including ones that will never succeed**
`@Retryable` with no `retryFor`/`noRetryFor` retries all `Throwable`s. That means a `404 Not Found` for a bad order id is retried 9 times, as is a `400`, a Jackson deserialization failure on a schema change, and an `NullPointerException` inside `Summary.of`. Restrict it:

```java
@Retryable(
    retryFor = { ResourceAccessException.class, HttpServerErrorException.ServiceUnavailable.class },
    noRetryFor = { HttpClientErrorException.class },
    maxAttempts = 3, ...)
```
(`ResourceAccessException` wraps `SocketTimeoutException`/`ConnectException`. If you want 408/429, add them explicitly — and honor `Retry-After` rather than the computed backoff, which `@Backoff` cannot do; that needs a `RetryListener` or a manual `RetryTemplate`.)

**4. Fixed 200 ms backoff with no jitter, no growth**
`@Backoff(delay = 200)` with no `multiplier` is a `FixedBackOffPolicy`, and `random` defaults to false. Every instance in the fleet retries at the same two offsets after a shared failure (a dependency restart, a network blip), producing a synchronized thundering herd. Use `@Backoff(delay = 100, multiplier = 2.0, random = true, maxDelay = 2000)`.

**5. No total deadline, so worst case is unbounded and threads pile up**
There is no cap on cumulative time across attempts. Even once timeouts exist, the budget is 9 × (0.5 s connect + 2 s read) + 1.6 s backoff ≈ 24 s of a held request thread — far past any sane upstream deadline. Decide the end-to-end budget for `summarize`, then size attempts and per-call timeouts to fit inside it, and confirm the caller's own timeout is longer than that figure (timeouts must nest).

**6. No circuit breaker or load shedding**
Blind retries against a dependency that is down convert a partial outage into a thread-pool exhaustion on this service. Wrap the client in a Resilience4j `CircuitBreaker` (or `Bulkhead`) so sustained failures fail fast instead of each request paying the full retry budget.

**7. Unvalidated `id` concatenated into the URI path**
`"/orders/" + id` lets `id` inject path segments (`../admin/...`), query strings, or `#`. Also, `getForObject` treats `{}` as URI-template placeholders, so an id containing a brace throws at expansion. Pass it as a variable and validate the format:

```java
http.getForObject("/orders/{id}", Order.class, id);  // id matches [A-Za-z0-9-]+
```

**8. Exhaustion is silent and unobservable**
No `@Recover`, no metrics, no log on retry. When the budget is spent, Spring Retry rethrows the last exception with no record that 9 attempts happened — you can't tell a single failure from a retry storm in the dashboards. Add a `@Recover` method with the deliberate degraded behavior (or an explicit domain exception) and register a `RetryListener` that counts attempts per dependency.

One thing that is fine as written: `fetch` is a GET, so it's idempotent and safe to retry. If `summarize` ever grows a write, that stops being true and the retry needs an idempotency key first.

</details>
