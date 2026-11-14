"""A Prometheus HTTP API over fixture series, for Sizing tests without a cluster.

It applies every label matcher in the query (=, !=, =~, !~) exactly as Prometheus
would, so a query that forgets its namespace or cluster matcher returns other
Contracts' series and the isolation tests catch it. PromQL functions around the
selector (rate, max_over_time, increase, last_over_time) are ignored: a fixture
series holds the values that query would return. `count(...)` counts matches. A
series added with `ago` (seconds) happened that long ago: an instant query over a
shorter `[...s]` window doesn't see it.

Three things an instant query does get evaluated (slice 15.1a), since what leaves
Prometheus depends on them: `* on (uid) group_left() max by (uid) (selector [== 1])`
keeps the series whose `uid` the other selector matches (with a value of 1), a selector
followed by `== 1` keeps those at 1, and an outer `sum by (...) (` or `count by (...) (`
returns one series per group with those labels only, as Prometheus does.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import httpx

SELECTOR = re.compile(r"([a-zA-Z_:][a-zA-Z0-9_:]*)\{([^}]*)\}")
JOIN = re.compile(
    r"\* on \(uid\) group_left\(\) max by \(uid\) \((" + SELECTOR.pattern + r")( == 1)?\)"
)
AGGREGATE = re.compile(r"(sum|count) by \(([^)]*)\) \(")
MATCHER = re.compile(r'\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*(=~|!~|!=|=)\s*"((?:[^"\\]|\\.)*)"\s*,?')


def _unquote(value: str) -> str:
    return re.sub(r"\\(.)", lambda m: {"n": "\n"}.get(m.group(1), m.group(1)), value)


def _matches(labels: dict[str, str], op: str, name: str, value: str) -> bool:
    have = labels.get(name, "")
    if op == "=":
        return have == value
    if op == "!=":
        return have != value
    hit = re.fullmatch(value, have) is not None
    return hit if op == "=~" else not hit


def _last(s: dict[str, Any]) -> Any:
    return s["_values"][-1] if isinstance(s["_values"], list) else s["_values"]


@dataclass
class FakePrometheus:
    series: list[dict[str, Any]] = field(default_factory=list)  # {"__name__", labels..., values}
    queries: list[str] = field(default_factory=list)
    status: int = 200

    def add(
        self, metric: str, values: Sequence[float | None] | float, ago: float = 0, **labels: str
    ) -> None:
        """A series from the window's start, a value a step; None: no sample then."""
        self.series.append({"__name__": metric, **labels, "_values": values, "_ago": ago})

    def select(self, promql: str) -> list[dict[str, Any]]:
        m = SELECTOR.search(promql)
        assert m, f"no selector in {promql!r}"
        name, body = m.groups()
        matchers = [(n, op, _unquote(v)) for n, op, v in MATCHER.findall(body)]
        assert "".join(MATCHER.sub("", body).split()) == "", f"unparsed matchers in {body!r}"
        return [
            s
            for s in self.series
            if s["__name__"] == name and all(_matches(s, op, n, v) for n, op, v in matchers)
        ]

    def joined(self, promql: str, found: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """`found` after the query's `== 1` on its own selector and its joins on uid."""
        own = SELECTOR.search(promql)
        assert own
        if promql[own.end() :].startswith(" == 1"):
            found = [s for s in found if _last(s) == 1]
        for m in JOIN.finditer(promql):
            uids = {
                s.get("uid") for s in self.select(m.group(1)) if not m.group(4) or _last(s) == 1
            }
            found = [s for s in found if s.get("uid") in uids]
        return found

    def handler(self, request: httpx.Request) -> httpx.Response:
        promql = request.url.params["query"]
        self.queries.append(promql)
        if self.status != 200:
            return httpx.Response(self.status, json={"status": "error", "error": "boom"})
        found = self.select(promql)

        def labels(s: dict[str, Any]) -> dict[str, str]:
            return {k: v for k, v in s.items() if not k.startswith("_") and k != "__name__"}

        if request.url.path.endswith("/query_range"):
            start, step = float(request.url.params["start"]), float(request.url.params["step"])
            result: list[dict[str, Any]] = [
                {
                    "metric": labels(s),
                    "values": [
                        [start + i * step, str(v)]
                        for i, v in enumerate(
                            s["_values"] if isinstance(s["_values"], Sequence) else [s["_values"]]
                        )
                        if v is not None
                    ],
                }
                for s in found
            ]
        elif promql.startswith("count("):
            result = [{"metric": {}, "value": [0, str(len(found))]}] if found else []
        else:
            window = re.findall(r"\[(\d+)s\]", promql)
            if window:
                found = [s for s in found if s["_ago"] <= int(window[-1])]
            found = self.joined(promql, found)
            if by := AGGREGATE.match(promql):
                groups: dict[tuple[tuple[str, str], ...], float] = {}
                for s in found:
                    g = tuple(
                        (k.strip(), s[k.strip()]) for k in by.group(2).split(",") if k.strip() in s
                    )
                    groups[g] = groups.get(g, 0.0) + (
                        1 if by.group(1) == "count" else float(_last(s))
                    )
                found = [{**dict(g), "_values": v} for g, v in groups.items()]
            result = [
                {
                    "metric": labels(s),
                    "value": [
                        0,
                        str(s["_values"][-1] if isinstance(s["_values"], list) else s["_values"]),
                    ],
                }
                for s in found
            ]
        kind = "matrix" if request.url.path.endswith("/query_range") else "vector"
        return httpx.Response(
            200, json={"status": "success", "data": {"resultType": kind, "result": result}}
        )

    def client(self) -> httpx.Client:
        return httpx.Client(
            base_url="http://prometheus", transport=httpx.MockTransport(self.handler)
        )
