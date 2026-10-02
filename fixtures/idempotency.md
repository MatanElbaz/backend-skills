```java
@PostMapping("/webhooks/transfer-settled")
public ResponseEntity<Void> onSettled(@RequestBody SettlementEvent e) {
    Account a = accounts.findById(e.accountId()).orElseThrow();
    a.setBalance(a.getBalance().add(e.amount()));
    accounts.save(a);
    return ResponseEntity.ok().build();
}
```
