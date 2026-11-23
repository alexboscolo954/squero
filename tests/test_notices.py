"""Stale notices (audit A14): a read failing 3 hours without a break is posted once to the
Contract's operator channel, reminded a day later in its thread, and closed with a reply
when it reads again; each post claimed in Postgres, a Contract's never another's."""

from typing import Any

import pytest

from squero import contracts, notices
from squero.store import Store
from tests.test_sizing_store import NOW, row, store, stored  # noqa: F401

H = 3600.0
CHANNEL = "C0123ABCDE"


def contract(cid: str = "acme", channel: str | None = CHANNEL) -> contracts.Contract:
    from tests.conftest import make_contract

    data = make_contract(contract_id=cid).model_dump()
    data["integrations"].append({"name": "prometheus", "url": "http://prometheus"})
    data["slack"] = {"operator_channel": channel} if channel else None
    return contracts.build(data)


class Posts:
    """Slack, as `notices.tick` posts: each post's channel, text and thread."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str | None]] = []
        self.refuse = False

    async def __call__(self, channel: str, text: str, thread: str | None) -> str | None:
        if self.refuse:
            return None
        self.sent.append((channel, text, thread))
        return f"ts-{len(self.sent)}"


async def look(st: Store, at: float, posts: Posts, c: contracts.Contract | None = None,
               url: str | None = None) -> None:  # fmt: skip
    await notices.tick(st, c or contract(), at, posts, url)


@pytest.mark.anyio
async def test_a_read_failing_three_hours_is_noticed_once_reminded_daily_then_closed(
    store: Store,  # noqa: F811
) -> None:
    await stored(store, "acme", NOW - H, {"test": None}, row())
    await stored(store, "acme", NOW, {"test": "ConnectError: Name or service not known"})
    posts = Posts()
    await look(store, NOW + 3 * H - 1, posts)
    assert posts.sent == []  # not yet three hours
    await look(store, NOW + 3 * H, posts, url="https://hub.example.ts.net/")
    (first,) = posts.sent
    assert first == (
        CHANNEL,
        ":warning: *[acme]* test: Sizing reads failing since 1972-09-27 00:00 UTC (3 h): "
        "ConnectError: Name or service not known\n"
        "<https://hub.example.ts.net/app/?contract=acme|Open the Now panel>",
        None,
    )
    await look(store, NOW + 3 * H + 600, posts)
    await look(store, NOW + 27 * H - 1, posts)
    assert len(posts.sent) == 1  # once, then quiet for a day
    await look(store, NOW + 27 * H, posts)
    assert posts.sent[1] == (
        CHANNEL,
        "Still failing, 1 day 3 h now: ConnectError: Name or service not known",
        "ts-1",
    )
    await stored(store, "acme", NOW + 128 * H, {"test": None}, row())
    await look(store, NOW + 128 * H, posts)
    assert posts.sent[2] == (
        CHANNEL,
        ":white_check_mark: test: Sizing reads work again, after 5 days 8 h",
        "ts-1",
    )
    await look(store, NOW + 129 * H, posts)
    assert len(posts.sent) == 3 and await store.stale_notices("acme") == []


@pytest.mark.anyio
async def test_a_post_slack_refused_is_tried_again(store: Store) -> None:  # noqa: F811
    await stored(store, "acme", NOW, {"test": "boom"})
    posts = Posts()
    posts.refuse = True
    await look(store, NOW + 3 * H, posts)
    posts.refuse = False
    await look(store, NOW + 3 * H + 600, posts)
    (first,) = posts.sent
    assert first[1].startswith(":warning: *[acme]* test: Sizing reads failing since")


@pytest.mark.anyio
async def test_a_read_back_before_three_hours_is_never_posted(store: Store) -> None:  # noqa: F811
    await stored(store, "acme", NOW, {"test": "boom"})
    posts = Posts()
    await look(store, NOW + H, posts)
    await stored(store, "acme", NOW + 2 * H, {"test": None}, row())
    await look(store, NOW + 2 * H, posts)
    await look(store, NOW + 9 * H, posts)
    assert posts.sent == [] and await store.stale_notices("acme") == []


@pytest.mark.anyio
async def test_a_bill_counts_from_when_the_hub_first_saw_it_fail(store: Store) -> None:  # noqa: F811
    await stored(store, "acme", NOW, {"test": None}, row())
    await store.fail_bill("acme", "test", "HTTP 403 from ce.us-east-1.amazonaws.com", NOW)
    posts = Posts()
    await look(store, NOW + H, posts)  # first seen: its own time unknown
    await look(store, NOW + 4 * H - 1, posts)
    assert posts.sent == []
    await look(store, NOW + 4 * H, posts)
    (first,) = posts.sent
    assert first[1] == (
        ":warning: *[acme]* test: Bill reads failing since 1972-09-27 01:00 UTC (3 h): "
        "HTTP 403 from ce.us-east-1.amazonaws.com"
    )


@pytest.mark.anyio
async def test_each_cluster_and_kind_has_its_own_thread(store: Store) -> None:  # noqa: F811
    await stored(store, "acme", NOW, {"test": "down"})
    await store.fail_bill("acme", "test", "boom", NOW)
    posts = Posts()
    await look(store, NOW, posts)
    await look(store, NOW + 3 * H, posts)
    assert len(posts.sent) == 2
    assert {p[1].split(": ")[2].split(" failing")[0] for p in posts.sent} == {
        "Sizing reads",
        "Bill reads",
    }
    await stored(store, "acme", NOW + 4 * H, {"test": None}, row())  # Sizing reads back
    await look(store, NOW + 4 * H, posts)
    (closed,) = posts.sent[2:]
    assert "Sizing reads work again" in closed[1]
    assert [r["kind"] for r in await store.stale_notices("acme")] == ["bill"]


@pytest.mark.anyio
async def test_without_a_slack_channel_nothing_is_posted(store: Store) -> None:  # noqa: F811
    await stored(store, "acme", NOW, {"test": "boom"})
    posts = Posts()
    await look(store, NOW + 9 * H, posts, contract(channel=None))
    assert posts.sent == [] and await store.stale_notices("acme") == []


@pytest.mark.anyio
async def test_a_contracts_failures_never_reach_another(store: Store) -> None:  # noqa: F811
    """Isolation: bank's failing read is posted to bank's channel only, kept as bank's."""
    await stored(store, "acme", NOW, {"test": None}, row())
    await stored(store, "bank", NOW, {"test": "theirs is down"})
    posts = Posts()
    for at in (NOW, NOW + 3 * H):
        await look(store, at, posts)
        await look(store, at, posts, contract("bank", "C0BANKCHAN1"))
    (only,) = posts.sent
    assert only[0] == "C0BANKCHAN1" and "*[bank]*" in only[1]
    assert await store.stale_notices("acme") == []


def test_an_offline_relays_notice_and_its_close_say_relay() -> None:
    f = notices.Failing("edge", "relay", NOW, NOW, "offline since 1972-09-27 00:00 UTC")
    text = notices.first(contract(), f, NOW, NOW + 3 * H, None)
    assert text == (
        ":warning: *[acme]* edge: its Relay is offline since 1972-09-27 00:00 UTC (3 h): the "
        "Hub can't read this cluster"
    )
    assert notices.reminder(f, NOW, NOW + 27 * H) == "Still failing, 1 day 3 h now"
    assert notices.recovered("edge", "relay", NOW, NOW + 49 * H) == (
        ":white_check_mark: edge: Its Relay is connected again, after 2 days 1 h"
    )


@pytest.mark.anyio
async def test_an_offline_relay_is_failing(store: Store) -> None:  # noqa: F811
    from tests.test_relay import A_TOKEN, relayed

    (f,) = await notices.failing(store, relayed("acme", A_TOKEN))
    assert (f.cluster, f.kind) == ("edge", "relay")


def test_a_span_is_hours_then_days() -> None:
    assert [notices._span(s * H) for s in (3, 24, 25, 48, 130)] == [
        "3 h",
        "1 day",
        "1 day 1 h",
        "2 days",
        "5 days 10 h",
    ]


def test_the_notice_escapes_what_it_quotes() -> None:
    f = notices.Failing("test", "sizing", NOW, NOW, "<!channel> boom")
    assert "&lt;!channel&gt; boom" in notices.first(contract(), f, NOW, NOW + 3 * H, None)


# --- the Hub ------------------------------------------------------------------------------


def test_the_hub_posts_to_the_operator_channel_and_only_with_a_bot_token(
    settings: Any, fake_cluster: Any, database_url: str
) -> None:
    import asyncio

    from pydantic import SecretStr

    from squero.store import connect
    from tests.test_api import client
    from tests.test_slack import ACME, TOKEN, FakeSlack

    async def seed() -> None:
        async with connect(database_url) as pool:
            s = Store(pool)
            await s.migrate()
            await stored(s, "acme", NOW, {"test": "boom"})

    asyncio.run(seed())
    settings.database_url = SecretStr(database_url)
    slack = FakeSlack()
    from tests.test_sizing_digest import PROM as prom

    with client(settings, fake_cluster, slack=slack.transport(), acme_channel=ACME,
                acme={"integrations": prom}) as c:  # fmt: skip
        assert c.portal is not None
        c.portal.call(c.app.state.due_notices, NOW + 3 * H)  # type: ignore[attr-defined]
    assert not [p for m, p, _ in slack.calls if m == "chat.postMessage"]  # no bot token
    settings.slack_bot_token = SecretStr(TOKEN)
    with client(settings, fake_cluster, slack=slack.transport(), acme_channel=ACME,
                acme={"integrations": prom}) as c:  # fmt: skip
        assert c.portal is not None
        c.portal.call(c.app.state.due_notices, NOW + 3 * H)  # type: ignore[attr-defined]
    (post,) = [p for m, p, _ in slack.calls if m == "chat.postMessage"]
    assert post["channel"] == ACME
    assert post["text"].startswith(":warning: *[acme]* test: Sizing reads failing since")


@pytest.mark.anyio
async def test_two_contracts_failing_on_a_cluster_of_one_name_stay_apart(
    store: Store,  # noqa: F811
) -> None:
    """Isolation, the mini PC's shape: both Contracts read a cluster named `test`. Each
    posts to its own channel in its own thread; one recovering closes only its own."""
    bank = contract("bank", "C0BANKCHAN1")
    await stored(store, "acme", NOW, {"test": "acme's down"})
    await stored(store, "bank", NOW, {"test": "bank's down"})
    posts = Posts()
    for at in (NOW, NOW + 3 * H):
        await look(store, at, posts)
        await look(store, at, posts, bank)
    assert [(p[0], p[2]) for p in posts.sent] == [(CHANNEL, None), ("C0BANKCHAN1", None)]
    assert "acme's down" in posts.sent[0][1] and "bank's down" in posts.sent[1][1]
    await stored(store, "acme", NOW + 4 * H, {"test": None}, row())
    await look(store, NOW + 4 * H, posts)
    await look(store, NOW + 4 * H, posts, bank)
    assert posts.sent[2] == (
        CHANNEL, ":white_check_mark: test: Sizing reads work again, after 4 h", "ts-1"
    )  # fmt: skip
    assert len(posts.sent) == 3  # bank's still failing, its day not yet out
    (theirs,) = await store.stale_notices("bank")
    assert (theirs["ts"], theirs["kind"]) == ("ts-2", "sizing")
    await look(store, NOW + 27 * H, posts, bank)
    assert posts.sent[3] == ("C0BANKCHAN1", "Still failing, 1 day 3 h now: bank's down", "ts-2")
    assert await store.stale_notices("acme") == []


@pytest.mark.anyio
async def test_a_cluster_no_longer_read_ends_quietly(store: Store) -> None:  # noqa: F811
    await stored(store, "acme", NOW, {"test": "down"})
    posts = Posts()
    await look(store, NOW + 3 * H, posts)
    data = contract().model_dump()
    data["integrations"] = [i for i in data["integrations"] if i["name"] != "prometheus"]
    gone = contracts.build(data)  # its Prometheus dropped: the cluster isn't read any more
    await look(store, NOW + 4 * H, posts, gone)
    assert len(posts.sent) == 1 and await store.stale_notices("acme") == []


@pytest.mark.anyio
async def test_a_bill_of_a_cluster_not_read_is_no_notice(store: Store) -> None:  # noqa: F811
    await store.fail_bill("acme", "elsewhere", "boom", NOW)
    posts = Posts()
    await look(store, NOW, posts)
    await look(store, NOW + 9 * H, posts)
    assert posts.sent == [] and await store.stale_notices("acme") == []


def test_one_contract_failing_stops_none_of_the_others(
    settings: Any, fake_cluster: Any, database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pydantic import SecretStr

    from tests.test_api import client
    from tests.test_sizing_store import prometheus_contract
    from tests.test_slack import TOKEN

    settings.database_url = SecretStr(database_url)
    settings.slack_bot_token = SecretStr(TOKEN)
    seen: list[str] = []

    async def tick(st: Any, c: Any, now: float, post: Any, url: Any, emit: Any) -> None:
        seen.append(c.id)
        if c.id == "acme":
            raise RuntimeError("boom")

    monkeypatch.setattr(notices, "tick", tick)
    more = {"bank": (prometheus_contract("bank"), fake_cluster)}
    with client(settings, fake_cluster, more=more) as c:
        assert c.portal is not None
        c.portal.call(c.app.state.due_notices, NOW)  # type: ignore[attr-defined]
    assert sorted(seen) == ["acme", "bank"]
