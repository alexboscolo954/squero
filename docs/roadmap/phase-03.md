# Phase 3: Relay

**In short:** Phase 3 added the Relay. It runs inside a client's cluster and dials out to the Hub, so the Hub needs no inbound route into the cluster. The Relay serves one cluster, and it forwards tool calls and alerts over that connection.

*Why it matters:* the Relay dials out, so the Hub needs no inbound route into the cluster. The Hub's tool calls and alerts still reach it.

**Status:** built (3.1 to 3.4; 3.3 split into 3.3a and 3.3b). Each slice's status is in the table below.

| Slice | What | Status |
|---|---|---|
| [3.1](#31-reversed-mcp-connection) | Reversed MCP connection over a websocket | ✅ built |
| [3.2](#32-relay-process) | The Relay process (`aiops relay`) | ✅ built |
| [3.3a](#33a-the-agents-calls-through-the-relay) | The agent's calls through the Relay | ✅ built, checked live |
| [3.3b](#33b-the-hubs-own-reads-through-the-relay) | The Hub's own reads through the Relay | ✅ built, checked live |
| [3.4](#34-alert-forwarding) | Alert forwarding | ✅ built, checked live |

## Decisions (grill, 2026-11-08)

Two rounds, all recommended. These decisions cover 3.4 and the Relay as a whole.

- **One cluster per Relay.** The Contract marks it `via: relay`. Its kubernetes tools and its Prometheus go through the Relay. So does the Hub's own PromQL (hourly Sizings, node facts, Health). So the optimizer sees a Relay cluster like a Direct one. Cloud and vendor integrations stay Direct.
- **Token.** It is a secret that the cluster names in the Contract's SOPS file (`relay_token_secret`). The same value sits in a Secret in the client cluster.
  - It is bound to that Contract and cluster.
  - It is compared in constant time.
  - The Hub refuses it at startup if it is shared, or equal to another token.
  - To rotate, change both and restart the Relay.
- **Dialing.** The Relay dials `wss://<domain>/relay`. This is a public Caddy route beside the alert route.
  - One live connection per token. A new connection replaces the old one.
  - It has idle and size limits.
- **Writes** need two switches. The Hub's gate (the Contract's `enable_actions` and the Operator's approval) must be on. The client's own switch on the Relay must also be on. That switch is off by default, and its RBAC overlay must match.
- **No queue.** An alert that reaches a Relay cut off from the Hub gets a 503. Alertmanager then retries. The Relay keeps no queue.
- **Health and the Agenda.** Health shows each Relay cluster as connected (since when, its version) or offline (since when). The Agenda lists an offline Relay as data the Hub cannot read.
- **Live checks** run on the local kind lab, with the Hub's direct kubeconfig removed.
- **Cost.** The demo eval for 3.4 ran on Haiku, for about 0.05 USD.

## 3.1 Reversed MCP connection

**What:**

- The Relay dials the Hub over WSS and runs the MCP server over that socket.
- The Hub runs a `ClientSession` on its end.
- Both SDK sides take raw streams. So this is an adapter, not a new protocol.
- Each Relay token is bound to one Contract.

**Done when:**

- it works over in-memory streams and over a real websocket;
- A's Relay token cannot serve B.

**Built (2026-11-08):** `relay.py`.

- mcp 2.3 has no websocket transport. So `bridge` turns a socket into MCP's stream pair. The socket is two callables: one sends a text frame, one receives one or `None`.
- Each text frame is one JSON-RPC message. This matches how MCP's stdio transport frames a line.
- A frame that is not one, or is over 4 MiB, reaches the session as an error. It ends nothing.
- `serve` runs a FastMCP server over the socket. That is the Relay.
- `RelayTransport` is a fastmcp Client. That is the Hub.

**Decided:**

- A cluster sets `via: relay` and `relay_token_secret`.
- Then it names none of the Hub's own credentials for it. These are: context, kubeconfig, `in_cluster`, `allow_exec`, and the Prometheus URL and token.
- Every Direct path skips a Relay cluster. A Direct path is one where the Hub holds the credentials and calls the cluster's API itself (see [CONTEXT.md](../../CONTEXT.md)). The Kubernetes tools, Sizing targets, probes, deploy polls and shared-store checks all skip it.
- The kube client refuses a Relay cluster outright. *Why:* with no context, the default one would be the Hub's.
- The Hub's `/relay` websocket names the Relay by its bearer token alone. The token is compared as a sha256, in constant time, with every token.
- Without a token, the socket is refused before it opens (403).
- The Hub keeps one live session per Contract and cluster. A new connection closes the old one.
- At startup the Hub refuses a Relay token that is under 32 characters, that is shared by two clusters, or that equals the approver, MCP or a webhook token.

**Tests:** in memory, and over a real websocket through uvicorn on loopback.

**Note:** the route is not on Caddy's public port until 3.3.

## 3.2 Relay process

**Plan:**

- `aiops relay` ships in the same image as the Hub.
- Manifests for the client cluster are provided.
- Credentials come from a Secret in the client cluster.
- The Hub hides any tool the Relay offers that is not on the Contract's allowlist.

**Built (2026-11-08):**

- `aiops relay` reads `AIOPS_RELAY_*` and serves one cluster.
- That cluster is a one-cluster Contract, built from those settings, with `in_cluster`.
- So the Hub's own Kubernetes and Prometheus servers run unchanged inside the client's network. They use the Hub's tool names.
- The namespaces are enforced there, as on the Hub.
- The Hub's own PromQL is served as `hub_promql_instant` and `hub_promql_range`. These are never on the agent's list. The Hub's allowlist leaves them out, from 3.3.
- Write tools are not registered unless the client sets `AIOPS_RELAY_WRITES`. This uses the `deploy/relay-actions` overlay, with RBAC for eviction and scale only.
- The Relay dials again after any drop. It waits 1 s, doubling up to a minute. After a connection that held for a minute, it goes back to 1 s.
- A refused token is logged as refused. The token itself is never logged.
- `deploy/relay` runs in namespace `aiops-relay`. It has a read-only ServiceAccount, bound per namespace. The token comes from a Secret. Its image is bumped with each release.

## 3.3 Hub side

**Plan:** a registry of connected Relays, reconnect with backoff, and a timeout per call.

- *Done when:* killing the Relay mid-Run ends in a Failed investigation ("Relay offline") within the timeout, not a hang.

**Split 2026-11-09:**

- **3.3a:** the agent's calls through the Relay (the Hub's checks, the timeout, "Relay offline", the public route).
- **3.3b:** the Hub's own reads through it (Sizings, node facts), and Relay state on Health and the Agenda.

## 3.3a The agent's calls through the Relay

**Built (2026-11-09):**

- A Relay's cluster is in the cluster list of the Hub's own Kubernetes and Prometheus tools.
- A Relay always offers Prometheus. A Relay without one answers that it has no such tool.
- `relay.Forward` is a middleware on the Contract's tool root, inside the audit.
- It takes a call to those tools that names a Relay's cluster.
- It checks the namespace against the Contract. The Relay's own list may be wider.
- It then makes the same call on that cluster's live Relay.
- `recent_changes` takes no namespace. So its answer is cut to the Contract's namespaces on the Hub. A GitOps object that deploys anywhere else is dropped whole. The text the model reads is rebuilt from what is kept.
- Any other tool stays local, including a Sizing. The Hub's local servers refuse a Relay's cluster outright.
- The Hub lists only its own tools. So the Relay's `hub_promql_*` and anything else it offers never reach the agent.
- No Relay connected, no answer within `AIOPS_RELAY_CALL_SECONDS` (30), or the socket closing under the call: each is "Relay offline". The Run fails after the call is recorded.
- A write needs the Relay's own switch too. If the Contract's `enable_actions` is on but the Relay's is off, the approved call errors.
- Caddy passes `GET /relay` with a websocket upgrade to the Hub, and nothing else there.
- `hub-smoke` checks that a Relay without a token, or with a wrong one, gets 403. A plain GET gets 404.

**Tests:** 23 mutations, all caught. Tests used Relays held in memory. They covered forwarding, the namespace check, two Contracts each with a cluster `edge`, a Relay that hangs, one killed mid-call (failed well under the timeout), and a Relay answering changes beyond the Contract.

**Checked live (2026-11-09, v0.30.0, Mac Hub and the kind lab, Haiku):**

- The Hub's own Caddyfile took the new route. `/relay` gave 403 without a token, and 404 for a plain GET.
- demo-shop's kind cluster was set to `via: relay`, with no kubeconfig on the Hub.
- `deploy/relay` ran in kind, with its three namespaces bound. It dialed `wss://<domain>/relay` through Caddy over TLS. The domain was aliased to the Mac, and Caddy's local CA was trusted.
- Questions were answered through it: 6 Kubernetes calls, including `recent_changes`. It found orders-api crash-looping on inventory's renamed `/stock`.
- Killing the Relay mid-Run came too late twice. The model's calls land within 100 ms of each other. So two other cases were checked instead:
  - A Relay force-deleted left the Hub's side half-open, until the websocket keepalive dropped it. The next Run failed at its first call ("has no Relay connected").
  - A Relay frozen with SIGSTOP failed the Run at the 30 s call timeout ("didn't answer in 30 s"). The call was recorded at 30,001 ms. It was not a hang.

**Found:**

- Sizings for a Relay's cluster errored until 3.3b.
- The Relay's RBAC could not read GitOps objects yet. So its `recent_changes` covered rollouts only.

The cluster was reverted to Direct and the Relay removed afterwards.

## 3.3b The Hub's own reads through the Relay

**Plan:** the Hub's own reads go through the Relay too. These are Sizings and node facts. Relay state shows on Health and on the Agenda.

**Built (2026-11-09):**

- A Relay's cluster is a Sizing target when the Contract has Prometheus. The target has no URL. `Target.relay` names its Contract and cluster.
- `usage.reader` reads it through `relay.Reader`. This calls the Relay's `hub_promql_instant` and `hub_promql_range`.
- The calls run on the loop that holds the Relay's connection, from the worker thread that usage reads run in. Each call has two minutes.
- When the Relay is offline, or an error result comes back, or the answer is not a series, the cluster gets a UsageError. The hourly run records that cluster's failure. The other clusters go on.
- The queries are the same as Direct. They are scoped to the Contract's namespaces.
- A Relay's Workloads come from metrics only, because the Hub has no Kubernetes API for it.
- Node facts and the Bill check's node-hours are read the same way.
- The Hub keeps one registry of Relays for the process (`relay.HUB`). The hourly tick, Health and the Agenda all read it.
- The registry remembers when each Relay went, and the version it reports. The version is shown but never trusted.
- Health has a row per Relay. It reads "connected since ..., version ...", "offline since ...", or "not connected since the Hub started at ...".
- Health's Kubernetes and Prometheus checks go through the Relay.
- The Agenda lists an offline Relay as data the Hub cannot read.
- `deploy/relay/rbac-gitops.yaml` is opt-in, like the Hub's. It lets the Relay's `recent_changes` read Argo CD and Flux objects.
- There is no API change.

**Tests:** 18 mutations, all caught.

**Checked live (2026-11-09, v0.31.0, Mac Hub and the kind lab, Haiku for one Question):**

- demo-shop's kind cluster was set to `via: relay`, with a fresh token.
- The Relay ran in kind, with `rbac-gitops.yaml` and its GitOps namespaces.
- Health showed "relay kind: connected since 15:25 UTC, version 0.31.0". Its kind Kubernetes and Prometheus checks passed through it, at 24 ms each.
- The hourly run at 15:43 UTC read kind through the Relay with no error. It stored 11 Sizings.
- The last Direct run with the lab up (2026-11-01) stored 12. The one missing was `demo/reports`. It had no usage samples since the lab restarted, because it was OOMKilled before cAdvisor saw it. So the Workloads were the same as Direct.
- The Agenda's waste items came from those Sizings.
- The Relay's ServiceAccount could list Argo CD Applications and Flux objects. None had changed in the last 7 days.
- The Relay was scaled to 0. Health then showed "offline since 15:46 UTC", with both checks "Relay offline". The Agenda's first item was "Relay offline since 2026-11-09 15:46 UTC: the Hub can't read cluster kind".

**Found:** `recent_changes` asks every GitOps kind in every GitOps namespace. Both the Hub's and the Relay's overlays granted Applications in `argocd`, and the Flux kinds in `flux-system`, only. So the others came back as 403 notices. The model read those as a missing grant. This also happened in Direct mode, before Phase 3.

The cluster was reverted to Direct and the Relay removed afterwards.

**GitOps grants fixed (2026-11-09, from the 3.3b live check):**

- Both overlays now grant all three GitOps kinds in each GitOps namespace.
- So the only 403 left in `recent_changes` is a real missing grant.
- A kind whose controller is not installed is still a 404, and is skipped.
- A test ties each overlay's Roles to `kubernetes.GITOPS`, the kinds that `recent_changes` reads. The test fails on the old overlays.
- There was no code change.

## 3.4 Alert forwarding

**What:** the Relay takes its cluster's Alertmanager webhook. It sends each alert group to the Hub over its connection.

**Done when:** on a kind cluster with no inbound route from the Hub, a full Triage through the Relay passes the demo eval.

**Built (2026-11-09):**

- The Relay takes its cluster's Alertmanager webhook. This is `POST /alertmanager` on its Service, port 8080.
- It sends each alert group to the Hub as a JSON-RPC request, `aiops/alert`. This request sits beside MCP. The bridge hands it aside, so the MCP session never sees it.
- Only one writer at a time uses the socket.
- The Relay passes on the bearer token that Alertmanager sent. It holds no secret for it.
- The Hub checks the token against the Relay's own Contract's webhook token. A token from another Contract, or the Relay token, gets a 401.
- The Hub then takes the group through the same path as the Contract's own route (`accept`). That is deduplication, related groups, the budget, and the Run. It answers once the group is stored.
- There is no queue. With no connection, no answer within 10 s, or the connection closing under it, the Relay returns a 503. If the Hub fails to take the group, it returns a 502. Alertmanager sends it again.
- A malformed body gets a 422. A body over 1 MiB gets a 413. Neither leaves the Relay.
- Nothing the model sees changed, so no eval was rerun.

**Tests:** 13 mutations caught; 2 equivalent mutants (the socket's lock, and the Relay's id check on answers).

**Checked live (2026-11-09, v0.32.0, Mac Hub and the kind lab, Haiku):**

- demo-shop's kind cluster was set to `via: relay`, with the Relay in kind.
- The lab's Alertmanager was turned on for the check. It routed KubePodCrashLooping in `orders` to the Relay's Service, with demo-shop's webhook token.
- The Hub was stopped just before the alert fired. Alertmanager got a 503 ("the Relay has no connection to the Hub") and retried.
- The Hub came back. The Relay redialed, and attempt 11 was taken (202), 14 s later.
- One Triage ran, with all 14 tool calls through the Relay. orders-api's startup sync got a 404 from inventory's `/stock`, while inventory served `/v2/stock` (`API_VERSION`).
- Against the demo Scenario's Expectation, the keywords passed. But the model chose `dependency_failure` over `bad_rollout`, with 14 calls (budget 12).
  - *Why:* the lab's inventory rollout is 21 days old, not minutes as in the Scenario.
  - The Report says no rollout explains it. That is right for the live cluster.

**Found:**

- v0.32.0's Relay did not start beside its own Service. Kubernetes sets `AIOPS_RELAY_PORT=tcp://...` in the pod, and that was read as its port setting. It was worked around for the check with `enableServiceLinks: false`. It was fixed below.
- With the Hub down, Caddy answers 502. The Relay logged that as "check the token".

Alertmanager was rolled back, the Relay removed, and demo-shop went back to Direct afterwards.

**Fix (2026-11-09):** the receiver's port setting is `AIOPS_RELAY_ALERTS_PORT`. No service link can set that name. A test sets every variable Kubernetes injects for the shipped Service and builds the settings. It fails on `port`.

**Log fix (2026-11-09):** a Relay that the Hub refuses says "check the token" only for 401 or 403. Caddy's 502, sent while the Hub is down, is logged as just the status. Two mutations caught.
