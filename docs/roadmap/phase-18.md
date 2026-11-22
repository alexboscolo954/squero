# Phase 18: Platform

**In short:** Phase 18 makes the cluster parts easy to install and safe to upgrade. One signed Helm chart installs the Relay and the Actuator. A Protocol number makes the Hub refuse a version mismatch and say who must upgrade. The Fleet page shows every Contract on one screen.

**Status:** complete (2026-11-21). Released in 0.62.0 (18.1) and 0.63.0 (18.3, 18.4). 18.1d shipped in 0.64.0.

**Goal:** install what runs in a client's cluster with one signed Helm chart, make the Hub and its cluster parts refuse a version mismatch clearly, and see every Contract on one page.

**Done when:** a fresh kind cluster goes from `helm install` to a connected Relay in one command, in CI. ✅ (18.1b, passed in CI's integration job)

| Slice | What | Status |
|---|---|---|
| [18.1a](#181a-the-chart) | The `squero` Helm chart replaces the kustomize directories | ✅ built |
| [18.1b](#181b-the-done-when-in-ci) | CI installs the chart into kind and sees the Relay connect | ✅ built, in CI |
| [18.1c](#181c-publish-and-sign) | Chart and image published to ghcr and signed with cosign | ✅ built, checked by 0.62.0 |
| [18.1d](#181d-no-npm-ci-under-qemu) | The console builds on the runner's own platform | ✅ built, checked by 0.64.0 |
| [18.3](#183-protocol) | A Protocol number per part; the Hub refuses a mismatch and says who must upgrade | ✅ built, not checked live |
| [18.4](#184-fleet) | The Fleet page: one row per Contract | ✅ built, not checked live |
| 18.2 | OpenShift values | Deferred (in the grill) |

## Decisions (grill, 2026-11-21)

Three rounds, all answers the recommended ones.

**Starting point:** the Relay and the Actuator installed from kustomize (`deploy/relay`, `deploy/actuator`). CI built the image for amd64 and arm64 on tags and pushed it to ghcr, unsigned. A Relay reported its version when it connected; an Actuator only reported the kinds it knows (`kinds=`, 12.8a).

- **Order:** 18.1, 18.3, 18.4. The done-when needs only 18.1.
- **One chart, `squero`**, with `relay.enabled` and `actuator.enabled`, each part in its own namespace as before.
  - It replaces the kustomize directories. `deploy-smoke` and the RBAC tests move to the rendered chart, so the RBAC lives in one place. *Why:* two copies is how one gets widened.
  - Tokens only by `existingSecret` names, never in values. *Why:* Helm keeps values in the cluster, and they end up in shell history.
  - The client creates each part's namespace and token Secret; the chart creates neither. *Why:* `helm uninstall` must never delete a token or the Actuator's ConfigMap.
- **Published** at `oci://ghcr.io/alexboscolo954/charts/squero`, its version the release's, by the tag run's `publish` job beside the image.
- **Signed:** the image and the chart with cosign keyless (GitHub's OIDC identity, logged in Sigstore's public log), plus the SBOM and provenance attestations. The docs show `cosign verify`.
- **Protocol** (a new term, 18.3) and **Fleet** (a new term, 18.4): see their slices.
- **The Hub's own install is unchanged:** Docker Compose (`deploy/hub`). The chart is only for what runs in a client's cluster.

## 18.1a The chart

**What:** `charts/squero`, replacing `deploy/relay`, `deploy/relay-actions` and `deploy/actuator`.

**Built (2026-11-21):**
- Version and appVersion are the release's. The image tag is the appVersion unless set.
- `relay.enabled` (on by default) and `actuator.enabled` (off).
- One `namespaces` list feeds every RoleBinding, the Relay's `SQUERO_RELAY_NAMESPACES` and the webhook's selector. An empty list is refused at render.
- Values:
  - Relay: `hub`, `cluster`, `prometheusUrl`, `writes`, `gitopsNamespaces`, `alertsPort`, `callSeconds`, `existingSecret`.
  - Actuator: `hub`, `interval`, `existingSecret`, and its three optional grants as switches, on as before (`nodes`, `nodeEvents`, `self`).
- No token is ever a value, and no Secret is ever rendered.
- `values.schema.json` refuses an unknown key.
- Names are fixed as the code expects (one release per cluster).
- The webhook's `caBundle` is never templated, so an upgrade keeps the Actuator's own certificate.

**Tests:** what the old directories rendered is kept in `tests/fixtures/rbac-0.61/`. Given the same values, the chart must render exactly those objects (except the two Namespaces), with writes, GitOps and each optional grant switched on one at a time. The chart tests need `helm` (v3.17.3 in CI). `demo/actuator-smoke.sh` installs with Helm and checks that `helm upgrade` keeps the webhook's certificate. 21 mutations, all caught.

## 18.1b The done-when in CI

**What:** `make deploy-smoke` installs the chart into kind, as a client would, and checks the Hub sees the Relay connect.

**Built (2026-11-21):**
- Adds a second cluster, `relayed` (`via: relay`), to the in-cluster Hub's Contract. Its token is random at each run, encrypted with the demo Contracts' synthetic test key, and the Contract is validated before it is applied.
- Creates the `squero-relay` namespace and token Secret, then runs one `helm install` with the Relay on.
- Passes when the Hub's last log line about that Relay is `relay in-cluster/relayed connected` within 90 seconds. Otherwise it prints the Relay's logs and fails (also shown in CI's failure step).
- Uninstalls the chart once it passes.

**Checked:** passes in CI (connected about 25 seconds after the install). Mutation on a throwaway kind cluster: one character added to the token makes it fail, with the Relay logging `the Hub refused the connection (403): check the token`.

## 18.1c Publish and sign

**What:** the tag run publishes the chart next to the image and signs both.

**Built (2026-11-21):**
- The `publish` job refuses a tag that isn't `Chart.yaml`'s `version` and `appVersion`, before anything is pushed.
- The image gets its SBOM and provenance (`mode=max`; its build args are versions only).
- Every action in the job is pinned to a commit SHA, with `.github/dependabot.yml` bumping them weekly. *Why:* a moved tag of any action would sign as the release.
  - `sigstore/cosign-installer` has no `v4` tag, so `@v4` would have failed.
- cosign keyless (v3.1.4) signs and then verifies the image's digest and the chart's digest, using the same commands [`client-cluster.md`](../client-cluster.md) shows. A green job proves the documented commands work.
  - The commands need cosign 3 or later. *Why:* v3 keeps a signature as an OCI 1.1 referring artifact.
- `Chart.yaml` names the repository in `home` and `sources`, which becomes the OCI source annotation.
- AGENTS.md's release steps wait for the chart and both signatures, then check the chart is public with no stored login. If that check is refused, the owner makes the package public on GitHub, then the check runs again.

**Recovering a failed run:** a failure after the image push → rerun the job (every step can run twice). A bug in the job itself → a fix commit and a patch tag.

**Checked:** by 0.62.0's tag run. Image and chart signed and verified, SBOM and provenance read, chart public (`helm show chart` returned `squero 0.62.0`). That release commit also found that the version bump broke the chart tests, because the reference fixtures pin the 0.61.0 image. `test_chart.py` now reads them at the chart's version.

## 18.1d No `npm ci` under QEMU

**Why it exists:** in 0.63.0's tag run, the arm64 build's `npm ci` hung silently under QEMU for 75 minutes (0.62.0's whole build took 3.5). The run was cancelled with nothing pushed, then rerun in 4 minutes. The `publish` job now stops after 30 minutes (`6f48ddb`).

**Built (2026-11-21):** `FROM --platform=$BUILDPLATFORM` on the Dockerfile's `console` stage. Its output is static files, the same for every architecture.

**Checked:** locally, the stage built for `linux/amd64` on the arm64 Mac ran natively (its `node` binary is AArch64). In CI by 0.64.0's tag run (2026-11-22): the image build took 3 minutes 6 seconds, the whole `publish` job 4 minutes 16 seconds.

## 18.3 Protocol

**What:** each cluster part (Relay, Actuator) speaks a Protocol number. The Hub accepts its own and the one before (never 0); anything else is refused with a message saying which side to upgrade.

**Decided:**
- The number starts at 1 for today's wire. It goes up only for a change an older part would misread: a field removed, renamed, or given a new meaning. An added optional field keeps the number (as `kinds=`, `eviction` and Blocker reports did).
- It travels in a `Squero-Protocol` header, on the Relay's websocket upgrade and on every Actuator call. No header means 1.
- A refusal is HTTP 426 carrying the Hub's own number.
- The Hub remembers a refusal in memory, by Contract, cluster and part, until that part's next accepted contact.
- The Agenda gets a new kind, `protocol`, after `stale`. Examples:
  - "Actuator in eu speaks Protocol 3, the Hub needs 4 or 5: upgrade it"
  - "Relay in eu speaks Protocol 6, this Hub 5: upgrade the Hub"
- The Actuator also sends `Squero-Version`, shown in Health and on the Fleet page.

**Built (2026-11-21):**
- `protocol.py`: each part's number, what the Hub accepts, the wording, and what each part last said (in memory).
- `/relay` checks the Protocol after the token (a bad token stays 403).
- The Actuator's routes check token and Protocol before the body is read. *Why:* a body a newer Protocol changed would otherwise get a 422 that the Actuator drops.
- Every answer carries the Hub's own number.
- On a 426, the Relay and the Actuator log which side must upgrade. The Actuator resends its reports and Blockers after 401, 403, 426 and 429.
- Health shows a Relay's Protocol on its connected line, and a line per Actuator (seen at, version, Protocol), FAILING only when refused. The console links a `protocol` item to Health.

**Tests:** 32 tests, including a real 426 over a socket, a 0.62.0 Relay with no header accepted, and two Contracts' Agendas and Health kept apart. 23 mutations, 21 caught, 2 equivalent.

**Found by the tests (behaviour changed):**
- A check inside the Actuator's route would let a body that a newer Protocol changed get FastAPI's 422, which the Actuator drops. The check is now a dependency that runs before the body is read.
- On the Actuator's routes a bad token now gets 401 before the body is read. Before, a bad body with a bad token got 422.
- A header value such as `²` passed `isdigit` and would have caused a 500. Only small ASCII numbers are read; anything else is unreadable.

**Known limits:**
- Not checked live: no 0.62.0 part has met the new Hub yet.
- A refused part that is then uninstalled stays on the Agenda until the Hub restarts.

## 18.4 Fleet

**What:** a console page, `/app/?view=fleet`, with one read-only row per Contract.

**Decided:**
- Each row shows: the Kill switch (shown, never turned on here), the Agenda's counts by kind, a month of waste and of requests in the Contract's own currency, Idle GPUs and Unrequested GPUs with their cost, and each cluster's Relay and Actuator with version and Protocol (a refused one in red).
- A row links to its Contract's Workloads page.
- Totals: one line per currency for the four costs, **never converted**. A Contract without prices and a GPU without a price stay out of the totals.
- Every Contract is computed on each load. One that fails shows its error on its own row.
- The Hub MCP server's `now()` already covers this across Contracts; nothing added there.

**Built (2026-11-21):** `GET /api/v1/fleet` (behind the console's identity and cookie) and `Fleet.tsx`. The page is read-only: no Contract picker and no Kill switch bar. Errors are redacted. A Contract that fails after its cost was read still stays out of the totals. The Hub MCP's `now()` names the Agenda's `protocol` item (18.3).

**Tests:** 5 tests (two currencies never summed, one Contract's numbers never another's, a failing Contract, GPU costs, another Contract's part never shown) and one console test. 14 mutations, all caught.

**Known limits:**
- Not checked live.
- Each load runs every Contract's Agenda (which reads Prometheus for its bill check) and reads Workloads twice. If the page gets slow, pass the Workloads in (a `ponytail:` note in the code).
