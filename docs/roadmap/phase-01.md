# Phase 1: Slack

**In short:** Phase 1 connected the Hub to Slack. Each Contract gets its own channel and each Investigation is one thread. The Operator approves or rejects from buttons, and can send a finished Report to the client. Questions can be asked by @mention.

*Why it matters:* the client channel never shows buttons, Evidence or Executed actions.

**Status:** done (4 of 4 slices).

| Slice | What | Status |
|---|---|---|
| [1.1](#11-slack-threads) | Slack threads, one per Investigation | ✅ done |
| [1.2](#12-approve-and-reject-buttons) | Approve and reject buttons | ✅ done |
| [1.3](#13-optional-client-delivery) | Optional client delivery | ✅ done |
| [1.4](#14-questions) | Questions, by @mention | ✅ done |

## 1.1 Slack threads

**What:**

- Each Contract has its own channel.
- Each Investigation is one thread. The thread shows its status, then the Report.
- This replaces `slack_webhook_url`.

**Decided:**

- Posting needs only the Web API (`chat.postMessage`, `chat.update`) with a bot token. So 1.1 uses httpx and no Slack library.
- Socket Mode receives events. It moves to 1.2, with the buttons.
- *Open at this point:* decide in 1.2 between keeping this poster and slack-bolt's client.

**Done when:** a fake Slack client shows that A's Investigations never post to B's channel.

## 1.2 Approve and reject buttons

**What:**

- The buttons use the same decision path as the API. The path is `settle`: claim, record, resume.
- A reject reason is entered in a modal. The pause is claimed only on submit. A cancelled modal decides nothing.
- Only the Operator's Slack user id is accepted, from `AIOPS_SLACK_OPERATOR_ID`. It is accepted only in the Investigation's own channel.

**Decided:**

- The buttons sit on the thread's parent, not on a reply.
- *Why:* the parent is re-rendered on every status change. So the buttons disappear on any decision or Stale, with no extra state.
- Each button carries its pause's deadline. So a click from an earlier pause cannot decide a later one.

**Built:** Socket Mode is a small loop over `websockets`. It acks each envelope and reconnects on `disconnect`. The httpx poster stays. slack-bolt was not needed.

**Done when:**

- a click from anyone but the Operator is refused;
- a second click is refused;
- going Stale updates the message.

## 1.3 Optional client delivery

**What:**

- Each Contract can set `slack.client_channel`. This is a Slack Connect channel.
- A finished Report gets a **Send to client** button in the Operator's thread.
- The Client report goes once, as plain text, with no controls.

The Client report holds the summary, root cause, severity, affected, and Recommendations. It never holds Evidence, Executed actions, the Runbook, or ids.

**Decided:**

- The send happens on the Operator's click, not automatically. *Why:* AI output is read before a client sees it.
- There is no Data policy field here. The click is the review. Masking stays in 6.4.

**Known limit:** a crash between claiming the send and posting it leaves the send marked as sent but never delivered. There is a `ponytail:` note in `api.py`.

**Done when:** delivery is off by default, and the client message never contains buttons.

## 1.4 Questions

**Part a, done.**

- The agent answers a Question with an Answer.
- Questions use read tools only. Write tools are hidden, not gated.
- A follow-up is a new message in the same thread.
- The eval runner takes Question Scenarios. Two pass. Their budgets were set from the runs. See [docs/development.md](../development.md#evals).

**Part b, done.**

- An @mention in a Contract's operator channel starts a Question. The Answer goes in the thread.
- An @mention in the thread continues the same Investigation.

**Decided:**

- Only messages that @mention the bot are read. The bot never reads other channel traffic.
- Questions only read.
- A Question always ends with an Answer.
- Mentions from anyone but the Operator, in a client channel, or in a Triage thread are refused privately.

**Built:**

- One Investigation per Slack thread, enforced by a unique index. A redelivered mention cannot start two.
- A follow-up claims a finished Question only with a newer mention. So two never run at once.
- A Question's thread root is the Operator's message. So the Hub only replies there.
- The Scenario and Expectation terms are widened, in the glossary and in the eval runner. A Scenario can start from a Question. Its Expectation checks the Answer's keywords and Evidence, not a Category.

**Done when:**

- two Question Scenarios pass;
- a Question in A's channel cannot reach B's clusters.
