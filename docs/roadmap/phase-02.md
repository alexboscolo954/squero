# Phase 2: Web console, Tailscale only

**In short:** Phase 2 added a web console, reachable only through Tailscale. The Operator can list Investigations, approve or reject, ask Questions, see costs and check health. A saved Investigation can be downloaded as a Scenario.

*Why it matters:* the console can change things, so it needs more than a login header. Every action also needs an approver token and a same-origin request.

**Status:** done (6 of 6 slices).

| Slice | What | Status |
|---|---|---|
| [2.1](#21-skeleton-and-login) | Skeleton and login | ✅ done |
| [2.2](#22-investigations) | Investigations page, approve and reject | ✅ done |
| [2.3](#23-ask-a-question) | Ask a Question from a form | ✅ done |
| [2.4](#24-cost) | Cost: tokens, spend and budgets | ✅ done |
| [2.5](#25-health) | Health | ✅ done |
| [2.6](#26-save-as-scenario) | Save as Scenario | ✅ done |

## 2.1 Skeleton and login

**What:**

- Login trusts the identity headers that `tailscale serve` sets.
- That is safe only because the console listens on `127.0.0.1` (slice 0.5).
- `/console` lists the Contracts for a login in `AIOPS_CONSOLE_OPERATORS`. Anyone else gets a 403.
- With no operators set, the console is not mounted (404). So the in-cluster deploy stays closed.
- Jinja autoescapes everything. Pages send a CSP and `frame-ancestors 'none'`.

**Tests:** `hub-smoke` proves that `/console` is a 404 through Caddy, even with a forged header. It also renders on `127.0.0.1` from the image.

**Known limit:** a real `tailscale serve` is not covered by CI.

**Done when:** a request without the Operator's Tailscale identity is refused.

## 2.2 Investigations

**What:**

- `/console` lists Investigations, filtered by Contract.
- Each Investigation's page shows the Report or Answer, the decisions, and every tool call. Each call shows its args and its redacted raw result.
- Approve and reject are plain forms. No HTMX is needed.
- Both go through the same decision path as the API and Slack.
- Each form names its pause (its deadline), like the Slack buttons.

**Acting needs more than the identity header.**

- `serve` only strips a *client's* copy of the header.
- Anything else that reaches the Hub's port could send it. That includes local processes and the other containers on the compose network.
- A network split does not stop this. Neither does tailscaled's `whois`, behind Docker's published port.
- So the header alone opens nothing but the token page.
- *Note:* since 2.3, a forged header could read client data from tool results.
- Every page and every POST also needs the **approver token**. It is pasted once per browser. It is kept as an HMAC of the token in a Secure, HttpOnly, SameSite=Strict cookie.
- Every request must be same-origin (`Sec-Fetch-Site`, or `Origin` checked against `Host`). This also stops CSRF.

## 2.3 Ask a Question

**What:**

- A form on `/console` takes a Contract and a question.
- **Follow up** on a Question's page adds a follow-up.
- Both use the same Question path as Slack: `start_question` and `follow_up`.
- The same rules apply as for acting: identity, the token cookie, and same origin.
- The page reloads itself while the Question runs. This is a meta refresh, with no script.
- A console Question has no Slack thread.

## 2.4 Cost

**Part a, done.**

- Every model call's tokens are recorded as the call happens. A `wrap_model_call` middleware does this. A resume never counts earlier Runs. A failed Run still counts.
- Each call is priced from `AIOPS_MODEL_PRICES`, in USD. Haiku's long-context tier is included.
- The console shows tokens and cost per Investigation, and spend per Contract this month.
- Contracts gain `model:` (through the Hub's gateway) and `monthly_budget_usd:`.
- A budget needs its model priced at startup.

**Part b, done.**

- Budgets apply only where a new Run starts: alerts, Questions and follow-ups. A resume never checks them.
- At 100%, the alert is still recorded. So repeats stay duplicates.
- At 100%, the Run fails with the spend and the budget. No model call is made. Slack still hears of it.
- The 80% and 100% notices post once per Contract per month. They are stored in `budget_notices`.
- *Known limit:* Runs that start together can overshoot the budget. The code has a `ponytail:` note about it.

**Done when:** at 100% of budget, a new alert still notifies the Operator, and its Failed investigation names the budget as the reason.

## 2.5 Health

**What:** the page shows:

- Relay connections;
- integration health probes;
- the last successful Investigation per Contract;
- the effective config, read-only, with secrets masked.

**Built:**

- `/console/health` runs one cheap read per integration. It uses the Contract's own tools (`agent.probes`). It runs on each load, with a 10 s limit each.
- Errors are redacted. Check results are never shown.
- Config shows each secret's name and whether it decrypted. It never shows values.
- Hub settings come out of pydantic with every `SecretStr` masked.

**Deviations:**

- There is no `health probe` field in `contract.yaml`. The builtin probes are enough until a vendor integration needs its own, in 4.1.
- Relay connections wait for Phase 3.

## 2.6 Save as Scenario

**What:** a finished Investigation becomes a Scenario YAML file. The Operator downloads it.

The Scenario holds the alert or Question, the recorded tool results, and a draft Expectation to edit. The Operator commits it to the contracts repo.

*Why:* the Hub never gets push access to a repo that holds every Contract's secrets.

**Built:**

- Each Investigation stores its input at intake (migration 0008).
- The download is `/console/investigations/<id>/scenario.yaml`. It needs identity plus the token.
- The Scenario is built from the recorded tool calls. Runbook tools are left out, because replay reads the files.
- A saved Scenario names its `clusters` and `integrations`. Replay builds exactly those, with placeholder config. So it offers the tools production did.
- Older Scenarios build byte-identical tools. This is checked by a fingerprint.
- `--runbooks` repeats, for the shared Runbooks plus the Contract's own.
- The eval runner takes `--scenarios <contracts-repo>/<contract>/scenarios`.

**Done when:** a saved Scenario replays to the same tool calls.
