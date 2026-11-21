---
status: accepted
---

# One Hub reasons for every Contract; Relays only run tools

**Decision in one line:** one Hub runs the agent's reasoning for every Contract, and a Relay inside a client network only runs tools.

## Decision

### Where a cluster is reached from

A Contract's cluster is reached in one of two ways:

- **Direct:** the Hub holds the cluster's credentials.
- **Through a Relay:** a Relay inside the client network.

### What a Relay does

- A Relay serves one cluster. The Contract marks it `via: relay`, and its token binds it to that Contract and cluster.
- It holds that cluster's credentials.
- It runs its Kubernetes and Prometheus servers under the Hub's own tool names.
- It answers the Hub's own PromQL.
- It dials out to the Hub.

The Hub then drives an MCP session over that outbound connection. So:

- the tools stay ordinary MCP servers;
- the client network accepts no inbound traffic;
- the cluster's Alertmanager posts to the Relay, which forwards each alert over the same connection.

Cloud and vendor integrations stay Direct.

### Who decides read or write

- The Hub, not the tool server, decides whether a tool reads or writes.
- Every vendor or Relay tool must be on the Contract's allowlist, with its read or write status stated there.
- Tools not on the allowlist are hidden from the agent.
- A server's own read-only annotation can only make a tool stricter, never looser. So a write tool mislabelled as read-only still waits for approval.

## Consequences

### Credential plugins in a kubeconfig

- A kubeconfig credential plugin (`exec`, or an `auth-provider` such as gcp's `cmd-path`) runs as a command on the Hub.
- It runs in the same process as every other Contract's secrets. A tampered client kubeconfig could read them all.
- So the Hub refuses such a kubeconfig, in two places:
  - in `squero contracts validate`;
  - before the Kubernetes client loads it.
- The only exception is a cluster that sets `allow_exec: true` in the contracts repo.
- An allowed plugin is still trusted with every Contract's secrets.
- For a Contract that needs a plugin and isn't fully trusted, use a Relay. It runs the plugin inside that client's network.

### Vendor MCP servers

The same reasoning holds for vendor MCP servers (slice 4.1):

- A Contract names a vendor from the Hub's registry.
- The command the Hub starts lives in the Hub's code, never in the contracts repo. So a push there can't run a program next to every Contract's secrets.
- The vendor process gets only MCP's safe default environment, plus the variables its registry entry allows.
- Those variables are set from its own Contract's config and secrets.
- A free choice of variables (`LD_PRELOAD`, `PATH`, ...) would run code all the same.

## Considered Options

- **The whole agent inside each client network.**
  - Tool output would never leave the client.
  - But it means N deployments to upgrade, an LLM key reachable from each client network, and knowledge and evals split per site.
  - Held back until a Contract forbids tool output leaving its network.
- **A central Hub with only Direct access.**
  - Simpler.
  - But it needs inbound access to every client network, which some clients refuse.
