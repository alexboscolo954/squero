"""Alert input (Alertmanager webhook v4, or Grafana Alerting's webhook), the triage
report the agent returns, and the Investigation record the Hub keeps."""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


class Alert(BaseModel):
    status: Literal["firing", "resolved"]
    labels: dict[str, str]
    annotations: dict[str, str] = {}
    startsAt: datetime | None = None
    generatorURL: str = ""
    fingerprint: str = ""


class AlertGroup(BaseModel):
    """Alertmanager webhook payload: one notification = one alert group."""

    version: str = "4"
    status: Literal["firing", "resolved"]
    receiver: str = ""
    groupKey: str = ""
    groupLabels: dict[str, str] = {}
    commonLabels: dict[str, str] = {}
    commonAnnotations: dict[str, str] = {}
    externalURL: str = ""
    alerts: list[Alert]

    @property
    def title(self) -> str:
        name = self.commonLabels.get("alertname", "alert")
        where = self.commonLabels.get("namespace", "")
        return f"{name} {where}".strip()

    @property
    def firing(self) -> list[Alert]:
        return [a for a in self.alerts if a.status == "firing"]


class GrafanaAlert(Alert):
    valueString: str = ""  # the rule's query values when it was evaluated


class GrafanaAlertGroup(AlertGroup):
    """Grafana Alerting's webhook contact point: Alertmanager's payload (same group
    key, fingerprints and resolved notifications) plus each rule's query values.
    Its rendered `title` and `message` repeat the labels and annotations: dropped."""

    alerts: list[GrafanaAlert]  # type: ignore[assignment]

    def normalized(self) -> AlertGroup:
        """An Alert group as Alertmanager would send it, its group key marked as
        Grafana's. The query values, the one thing it adds, stay visible to the agent
        as the `query_values` annotation."""
        alerts = [
            Alert(
                **a.model_dump(exclude={"valueString", "annotations"}),
                annotations={"query_values": a.valueString, **a.annotations}
                if a.valueString
                else a.annotations,
            )
            for a in self.alerts
        ]
        # Its own group keys: an Alertmanager grouping by the same labels would send
        # the same key, and one source resolving must not stale the other's actions.
        fields = self.model_dump(exclude={"alerts", "groupKey"})
        return AlertGroup(**fields, groupKey=f"grafana:{self.groupKey}", alerts=alerts)


# Root cause class, not failure mode (docs/adr/0001-category-is-root-cause-class.md).
Category = Literal[
    "bad_rollout",
    "bad_config",
    "app_error",
    "resource_limits",
    "image_unavailable",
    "capacity",
    "node_problem",
    "dependency_failure",
    "unknown",
]


class Evidence(BaseModel):
    source: str = Field(
        description="The one tool that produced it, exactly as named in your tools, e.g. "
        "kubernetes_pod_logs. No arguments and no second tool: say which pod or query in "
        "the finding, and give each tool its own Evidence."
    )
    finding: str = Field(description="What it showed, quoted or summarized, one line")
    link: str | None = Field(
        None,
        description="A link a tool returned that opens this evidence (e.g. from a "
        "generate_deeplink tool), copied exactly. Never build or edit one.",
    )

    def to_markdown(self) -> str:
        return f"- `{self.source}`: {self.finding}" + (f" ({self.link})" if self.link else "")


class TriageReport(BaseModel):
    """Result of investigating one alert group. Every claim must be backed by evidence."""

    summary: str = Field(description="One or two sentences an on-call engineer reads first")
    category: Category = Field(
        description="Why it broke, not how it fails. bad_rollout: a new revision broke a "
        "workload whose earlier revision worked, including a rollout of another workload "
        "the alerting one depends on (wins over the other classes). dependency_failure: "
        "something it depends on is failing, with no rollout to blame. bad_config: "
        "wrong or missing config, command or probe. app_error: bug in the code, including "
        "memory leaks. resource_limits: limits too low for a healthy workload. "
        "image_unavailable: tag or registry problem. capacity: the cluster can't fit the workload."
    )
    probable_root_cause: str = Field(
        description="Why it broke, with the deciding facts from the evidence: the value, "
        "limit, version, revision or time that proves it (e.g. memory limit 64Mi against "
        "a heap that needs about 200Mi), not only a summary of the change"
    )
    confidence: Literal["high", "medium", "low"] = Field(
        description="high: direct evidence (e.g. the error in logs); low: inferred"
    )
    severity: Literal["critical", "high", "medium", "low"]
    affected: list[str] = Field(description="Workloads affected, as namespace/kind/name")
    evidence: list[Evidence] = Field(min_length=1)
    recommended_actions: list[str] = Field(description="Ordered, concrete next steps")
    runbook: str | None = Field(None, description="Runbook name used, if one matched")

    def to_markdown(self, executed_actions: list[str] | None = None) -> str:
        """executed_actions come from the system's record, never from the model."""
        lines = [
            f"## {self.summary}",
            "",
            f"**Root cause** ({self.confidence} confidence, `{self.category}`): "
            f"{self.probable_root_cause}",
            f"**Severity:** {self.severity}  ·  **Affected:** {', '.join(self.affected) or '-'}",
            "",
            "**Evidence**",
            *[e.to_markdown() for e in self.evidence],
            "",
            "**Recommendations**",
            *[f"{i}. {a}" for i, a in enumerate(self.recommended_actions, 1)],
        ]
        if executed_actions:
            lines += ["", "**Executed actions**", *[f"- `{a}`" for a in executed_actions]]
        if self.runbook:
            lines += ["", f"Runbook: `{self.runbook}`"]
        return "\n".join(lines)


class Answer(BaseModel):
    """Result of a Question: a direct reply. Every claim must be backed by evidence."""

    answer: str = Field(description="The direct reply to the Operator's question, in markdown")
    evidence: list[Evidence] = Field(min_length=1)

    def to_markdown(self) -> str:
        return "\n".join(
            [
                self.answer,
                "",
                "**Evidence**",
                *[e.to_markdown() for e in self.evidence],
            ]
        )


class CheckResult(BaseModel):
    """Result of a scheduled Check: does what the Operator wrote hold right now?
    Every claim must be backed by evidence."""

    passed: bool = Field(
        description="true only when the evidence shows the Check holds; false when it "
        "doesn't, or when you couldn't tell (a tool failed, a target is missing)"
    )
    summary: str = Field(description="One or two sentences: what holds, or what doesn't")
    evidence: list[Evidence] = Field(min_length=1)
    report: TriageReport | None = Field(
        None,
        description="Required when passed is false, empty when true: the failure "
        "investigated like an alert",
    )

    @model_validator(mode="after")
    def _report_on_failure(self) -> "CheckResult":
        if not self.passed and self.report is None:
            raise ValueError("a failing Check needs its report")
        if self.passed:
            self.report = None
        return self

    def to_markdown(self) -> str:
        verdict = "Passed" if self.passed else "Failed"
        return "\n".join(
            [f"**{verdict}:** {self.summary}", "", "**Evidence**"]
            + [e.to_markdown() for e in self.evidence]
        )


class Investigation(BaseModel):
    id: str
    kind: Literal["triage", "question", "check"] = "triage"
    contract_id: str
    title: str
    group_key: str = ""
    group_resolved: bool = False
    status: Literal["running", "awaiting_approval", "done", "failed"] = "running"
    started: float  # epoch seconds, Python's clock
    report: TriageReport | None = None
    answer: Answer | None = None  # a Question's latest
    check_result: CheckResult | None = None  # a Check's verdict; its Report when failed
    pending: list[dict[str, Any]] = []
    executed_actions: list[str] = []
    error: str | None = None
    # When this pause's Proposed actions go Stale. Also identifies the pause: a
    # decision claims it only while it is still the same pause.
    deadline: float | None = None
    # The Slack thread this Investigation posts to, once its parent message exists.
    slack_channel: str | None = None
    slack_ts: str | None = None
    # When the Client report was sent, and by whom (never set by a Run).
    client_sent_at: float | None = None
    client_sent_by: str | None = None
    jira_issue: str | None = None  # its key in the Contract's Jira project
    jira_by: str | None = None
    jira_at: float | None = None  # claimed; with no key yet: being filed, or unknown
    servicenow_incident: str | None = None  # its number in the Contract's ServiceNow
    servicenow_id: str | None = None  # its sys_id: the link
    servicenow_by: str | None = None
    servicenow_at: float | None = None  # claimed; with no number yet: being filed, or unknown
    # A Question's last @mention (Slack ts): a follow-up must be newer.
    last_mention_ts: str | None = None
    # What started it, as received (written once): the Alert group, or
    # {"question": text} for a Question's first message, {"check": name, "prompt":
    # text} for a Check. None before migration 0008.
    input: dict[str, Any] | None = None
