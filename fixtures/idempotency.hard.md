```java
@PostMapping("/transfers")
public ResponseEntity<TransferResponse> create(
        @RequestHeader("Idempotency-Key") String key,
        @RequestBody TransferRequest req) {
    var existing = keys.findById(key);
    if (existing.isPresent()) {
        return ResponseEntity.ok(existing.get().getResponse());
    }
    TransferResponse res = transfers.execute(req);
    keys.save(new IdempotencyKey(key, res));
    return ResponseEntity.ok(res);
}
```
