# Category is the root cause class, not the failure mode

**Decision in one line:** a Report's Category names the root cause class, which maps to a kind of fix, not the way the thing failed.

## Context

- The alert name already carries the failure mode: `oom_killed`, `image_pull`, crash loop.
- On-call needs the cause class instead.
- The cause class maps to a kind of fix: rollback, config change, capacity.

## Decision

- A Report's Category says **why** something broke. It is the root cause class, such as `bad_rollout`, `bad_config`, `resource_limits`, ...
- It does **not** say how the thing failed.
- When a rollout introduced the fault after an earlier revision that worked, the Category is `bad_rollout`. This holds even if the fault itself is bad config or a bad image.

## Alternatives rejected

- **Classify by failure mode.** It duplicates the alert name.
- **Split into two fields: failure mode and cause.** It doubles what every Expectation has to pin down.
