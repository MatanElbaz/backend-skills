---
name: timeouts-and-retries
description: Use when code calls another service, database, or queue over the network, or when adding retry logic
---

# Timeouts and retries

A call without a timeout waits as long as the other side does. A retry without limits turns one slow dependency into an outage for everyone.

## When to use

- Any outbound HTTP, gRPC, database, cache, or broker call.
- Adding, reviewing, or tuning retry or backoff logic.

## Checklist

1. Every outbound call has an explicit connect timeout and a read or total timeout. Library defaults are often infinite or minutes long.
2. Timeouts nest: a downstream timeout is shorter than the upstream timeout that is waiting for it, and the whole chain fits inside the caller's deadline.
3. Retry only operations that are idempotent or carry an idempotency key, and only on transient failures: timeouts, connection resets, 503. Never on 4xx (except 408 and 429, honoring `Retry-After`), and never on exceptions that signal a bug.
4. Backoff is exponential with jitter, with a maximum number of attempts and a maximum total time. Without jitter, clients retry in lockstep.
5. Retry at one layer only. Three layers that each try three times make 27 calls.
6. Protect the dependency: a circuit breaker or load shedding, and honor `Retry-After`.
7. Queues and thread pools are bounded, and the behavior when they are full is chosen on purpose (reject, shed, or block with a limit).

## Bad example

```java
String callPricing(String sku) {
    while (true) {
        try {
            return http.get("/prices/" + sku);
        } catch (Exception e) {
            // try again
        }
    }
}
```

## What to flag

- Infinite loop: one dead dependency pins this thread forever.
- No timeout on the call, so a hung connection never throws and never retries.
- No delay between attempts: the loop hammers a dependency that is already struggling.
- Catches `Exception`, so it also retries bugs and 4xx responses that will never succeed.
- No jitter: every instance retries at the same moments.

## Good example

```java
HttpClient client = HttpClient.newBuilder()
    .connectTimeout(Duration.ofMillis(500))
    .build();

class RetryableStatusException extends RuntimeException {
    RetryableStatusException(int status) { super("HTTP " + status); }
}
class PermanentStatusException extends RuntimeException {
    PermanentStatusException(int status) { super("HTTP " + status); }
}

RetryConfig config = RetryConfig.custom()
    .maxAttempts(3)
    .intervalFunction(IntervalFunction.ofExponentialRandomBackoff(100, 2.0, 0.5))
    .retryOnException(e -> e instanceof HttpTimeoutException
                        || e instanceof ConnectException
                        || e instanceof RetryableStatusException)
    .build();
Retry retry = Retry.of("pricing", config);

String callPricing(String sku) throws Throwable {
    HttpRequest req = HttpRequest.newBuilder(URI.create(baseUrl + "/prices/" + sku))  // sku already validated as [A-Za-z0-9-]+; never put unchecked input in a URI
        .timeout(Duration.ofSeconds(2))
        .GET()
        .build();
    return retry.executeCheckedSupplier(() -> {
        HttpResponse<String> res = client.send(req, HttpResponse.BodyHandlers.ofString());
        int status = res.statusCode();
        if (status == 503 || status == 429) throw new RetryableStatusException(status);
        if (status >= 400) throw new PermanentStatusException(status);
        return res.body();
    });
}
```

Resilience4j is shown, but the rules do not depend on the library. Only timeouts, refused connections, and 503 or 429 are retried. Any other 4xx fails immediately. This example does not read `Retry-After`; if the service sends one, wait at least that long instead of the computed backoff. A connection reset arrives as a plain `IOException` and is not retried here; add it for idempotent calls. With at most 3 attempts of 500 ms connect plus 2 s for the response headers, plus under half a second of backoff, the worst case is under 8 seconds. That bound covers time to response headers: `HttpRequest.timeout` does not cap a slow body read, so cap that separately if bodies can be large. The figure must fit inside the caller's own deadline. A GET is safe to retry. A POST needs an idempotency key first.
