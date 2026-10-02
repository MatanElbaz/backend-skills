# backend-skills

Five Claude Code skills that check backend code against common production failure modes.

Five skills for Claude Code, each a checklist plus a bad and good example for a class of production failure: idempotency, transaction boundaries, timeouts and retries, safe migrations, and money handling. Examples are Java/Spring and PostgreSQL.

## Install

As a Claude Code plugin:

```
/plugin marketplace add MatanElbaz/backend-skills
/plugin install backend-skills@backend-skills
```

Or without the plugin, copy [`snippets/CLAUDE.md`](snippets/CLAUDE.md) into your project's `CLAUDE.md` or `AGENTS.md`.

## What's inside

| Skill | Use it when |
|---|---|
| [`idempotency`](skills/idempotency/SKILL.md) | An endpoint, webhook, consumer, or job can run more than once |
| [`transaction-boundaries`](skills/transaction-boundaries/SKILL.md) | Database writes sit next to HTTP calls, events, or emails |
| [`timeouts-and-retries`](skills/timeouts-and-retries/SKILL.md) | Code calls anything over the network, or adds retries |
| [`safe-migrations`](skills/safe-migrations/SKILL.md) | A migration touches a large or busy table |
| [`money-handling`](skills/money-handling/SKILL.md) | Code stores, computes, or compares amounts |

Each skill has the same shape: when to use it, a checklist, a bad example, what a reviewer should flag in it, and a good example.

## Does it change the output?

Not by the measure I used.

For each skill I asked the same review question about a different piece of code than the one in the skill, three times without the plugin and three times with it, and counted the runs whose answer mentioned something specific to that skill's checklist. Every run and the exact signal used are in [`docs/demos/`](docs/demos).

<!-- results:start -->
| Skill | Without plugin | With plugin |
|---|---|---|
| `idempotency` | 3 / 3 | 2 / 3 |
| `money-handling` | 3 / 3 | 3 / 3 |
| `safe-migrations` | 3 / 3 | 3 / 3 |
| `timeouts-and-retries` | 3 / 3 | 3 / 3 |
| `transaction-boundaries` | 3 / 3 | 3 / 3 |
<!-- results:end -->

Counts are runs out of 3. Without the plugin the model already raised the point every time, so this test shows no improvement. Idempotency scored lower with the plugin (2 of 3 against 3 of 3), which at this sample size is noise. Not captured: in the recorded outputs the plugin's skills are used as the review checklist (most answers name the skill they applied), and I have not measured whether that makes reviews better. Read the outputs and judge for yourself.

The test is weak on purpose-built grounds: three runs, one model, keyword matching that measures whether a point was mentioned and not whether the advice was right, and the "with" runs may also include other plugins installed on the machine. Reproduce with `scripts/demo.sh <skill> 3`. Harder, subtler cases would be a fairer test and are the next thing to try.

## Limitations

- Examples are Java 17+ and PostgreSQL. The checklists apply elsewhere, the code does not.
- These are review heuristics, not a replacement for a review by someone who knows your system.
- The skills describe common failure modes. They do not know your architecture.
- No measured improvement yet. See the section above.

## Contributing

Open an issue with the [skill proposal template](.github/ISSUE_TEMPLATE/skill-proposal.md). Run `python3 scripts/check_skills.py` before sending a pull request.

## License

MIT
