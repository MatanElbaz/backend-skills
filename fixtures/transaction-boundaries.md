```java
@Transactional
public void register(User u) {
    users.save(u);
    emailClient.sendWelcome(u.email());
    events.publish(new UserRegistered(u.id()));
}
```
