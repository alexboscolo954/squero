"""Runtime settings, read from SQUERO_* environment variables (or a .env file)."""

import os
from pathlib import Path
from typing import Literal

from dotenv import dotenv_values
from pydantic import BaseModel, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class LongContext(BaseModel):
    over: int = Field(gt=0)  # a prompt over this many tokens pays the prices below
    input: float = Field(ge=0)
    output: float = Field(ge=0)


class Price(BaseModel):
    """USD per million tokens. Priced per model call, as providers bill: a call whose
    prompt is over `long.over` tokens pays the long-context prices for the whole call.
    ponytail: cache discounts are ignored, so a cost reads a little high (the safe
    side for a budget); add them if the gap matters."""

    input: float = Field(ge=0)
    output: float = Field(ge=0)
    long: LongContext | None = None

    def cost(self, input_tokens: int, output_tokens: int) -> float:
        rate = self.long if self.long and input_tokens > self.long.over else self
        return (input_tokens * rate.input + output_tokens * rate.output) / 1_000_000


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SQUERO_", env_file=".env", extra="ignore")

    # LLM. Any init_chat_model string; set model_base_url for an
    # OpenAI-compatible gateway (LiteLLM, vLLM) and use "openai:<model>".
    model: str = "anthropic:claude-opus-5-5"  # audit A12: Sonnet 5.5 loops on Questions
    model_base_url: str | None = None
    model_api_key: SecretStr | None = None
    model_headers: dict[str, str] = {}  # JSON, e.g. {"anthropic-workspace-id": "..."}
    # How much the model reasons (Anthropic's output_config.effort; reasoning_effort on an
    # OpenAI-compatible gateway). Unset: the model's own default.
    model_effort: Literal["low", "medium", "high", "xhigh", "max"] | None = None
    # $/MTok per model string (JSON), for costs and budgets. A Contract with a budget
    # needs its model priced; others just show tokens.
    model_prices: dict[str, Price] = {}
    max_model_calls: int = 30
    max_tool_calls: int = 25
    tool_output_limit: int = 8000  # max characters per tool result
    relay_call_seconds: float = 30  # a Relay that doesn't answer a call in time is offline
    # Similar incidents by meaning (embeddings.py): an embedding model on the model
    # gateway, e.g. openai/text-embedding-3-small on OpenRouter. Unset: words only.
    embedding_model: str | None = None
    # Its own gateway and key, for a model called directly (Anthropic has no embeddings
    # API). Unset: the model's gateway, key and headers.
    embedding_base_url: str | None = None
    embedding_api_key: SecretStr | None = None
    # Cosine distance under which a past Report counts as similar. ponytail: set from
    # six short probes on text-embedding-3-small (2026-10-20); tune on real Reports.
    embedding_max_distance: float = Field(0.6, gt=0, le=2)

    # Every Contract: one directory each (see contracts.py). Required for serve,
    # triage and mcp.
    contracts_dir: Path | None = None

    # Webhook server. Each Contract's Alertmanager has its own token (a hash in its
    # contract.yaml) and can only submit alerts; only the Operator's approver token
    # reads reports and approves actions.
    approver_token: SecretStr | None = None
    database_url: SecretStr | None = None  # Postgres; required for serve
    max_concurrent_triages: int = 3  # alert storms queue instead of fanning out
    decision_timeout_seconds: int = 1800  # proposed actions go stale after this
    # How long after a Triage starts a new Alert group can still join it (a Contract's
    # related.window_minutes overrides it).
    related_window_minutes: int = 30
    slack_bot_token: SecretStr | None = None  # xoxb-...; channels are per Contract
    # Approve / reject buttons: Socket Mode with an app-level token (xapp-...,
    # connections:write). Only clicks from the Operator's Slack user id decide.
    slack_app_token: SecretStr | None = None
    slack_operator_id: str | None = None  # U...

    # Web console: the Tailscale logins (JSON list) that may sign in. Empty = off.
    # Only behind `tailscale serve`: anything else reaching the port could send the
    # identity header itself (console.py).
    console_operators: set[str] = set()
    # The console's address (the `tailscale serve` URL): the weekly digest links its
    # Workloads page. Unset: no link.
    console_url: str | None = None
    # The Hub MCP server at /mcp/ (slice 11.1a): this bearer token and a listed console
    # Operator's Tailscale identity, so it needs SQUERO_CONSOLE_OPERATORS. Unset = off.
    mcp_token: SecretStr | None = None
    # Proposed changes (slice 11.3): the private contracts repo on GitHub (owner/name,
    # cloned at SQUERO_CONTRACTS_DIR) and a fine-grained token for it alone (contents
    # and pull requests, read and write). Unset = the Hub proposes nothing.
    contracts_repo: str | None = None
    contracts_token: SecretStr | None = None

    @model_validator(mode="after")
    def _embeddings_need_a_gateway(self) -> "Settings":
        if self.embedding_model and not (self.embedding_base_url or self.model_base_url):
            raise ValueError(
                "SQUERO_EMBEDDING_MODEL needs a gateway: SQUERO_EMBEDDING_BASE_URL, "
                "or the model's SQUERO_MODEL_BASE_URL"
            )
        return self


# Settings that are gone: most moved into a Contract's contract.yaml (slice 0.3).
MOVED = {
    "SQUERO_IN_CLUSTER": "clusters[].in_cluster",
    "SQUERO_KUBECONFIG": "clusters[].kubeconfig_file",
    "SQUERO_KUBE_CONTEXT": "clusters[].context",
    "SQUERO_NAMESPACES": "clusters[].namespaces",
    "SQUERO_ENABLE_ACTIONS": "the kubernetes integration's enable_actions",
    "SQUERO_MAX_REPLICAS": "the kubernetes integration's max_replicas",
    "SQUERO_PROMETHEUS_URL": "the prometheus integration's url",
    "SQUERO_PROMETHEUS_TOKEN": "the prometheus integration's token_secret",
    "SQUERO_RUNBOOKS_DIR": "<contract>/runbooks and shared/runbooks",
    "SQUERO_WEBHOOK_TOKEN": "webhook_token_sha256 (squero contracts new-token)",
    "SQUERO_SLACK_WEBHOOK_URL": (
        "SQUERO_SLACK_BOT_TOKEN, plus each Contract's slack.operator_channel"
    ),
}


def refuse_moved(env_file: Path | None = Path(".env")) -> None:
    """Settings ignores unknown variables, so a deploy still setting these would
    lose them silently. Fail and say where each one went."""
    names = set(os.environ)
    if env_file and env_file.is_file():
        names |= set(dotenv_values(env_file))
    found = sorted(set(MOVED) & names)
    if found:
        where = "; ".join(f"{n} -> {MOVED[n]}" for n in found)
        raise RuntimeError(f"these settings are gone; set instead: {where}")
