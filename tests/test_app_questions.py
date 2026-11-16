"""Questions and the Contracts overview in the console app's API (slice 11.7a): ask about
one Contract, follow up on a Question no longer answering, and every Contract at a
glance with its own spend."""

import asyncio
from typing import Any

from pydantic import SecretStr

from squero.config import Settings
from squero.models import Investigation
from squero.store import Store, connect
from tests.conftest import REPORT, call, make_contract
from tests.test_api import ALERT, AUTH, HOOK, URL, client
from tests.test_app_investigations import SAME
from tests.test_console import ANSWER_1, ANSWER_2, AS_OPERATOR, OPERATOR, VIEW
from tests.test_cost import HAIKU
from tests.test_restart import wait_for

ASK = "/api/v1/contracts/acme/questions"
CROSS = {**VIEW, "Sec-Fetch-Site": "cross-site"}


def test_a_question_from_the_app_answers_and_follows_up_without_slack(
    settings: Settings, fake_cluster: Any, database_url: str
) -> None:
    from tests.test_slack import ACME, FakeSlack

    settings.database_url = SecretStr(database_url)
    settings.console_operators = {OPERATOR}
    settings.slack_bot_token = SecretStr("xoxb-test")
    slack = FakeSlack()
    steps = [call("Answer", "a1", **ANSWER_1), call("Answer", "a2", **ANSWER_2)]
    with client(
        settings, fake_cluster, *steps, actions=True, slack=slack.transport(), acme_channel=ACME
    ) as c:
        q = {"question": "  is checkout healthy?  "}
        refused = [
            c.post(ASK, json={"question": " "}, headers=SAME).status_code,
            c.post(ASK, json={"question": "x" * 4001}, headers=SAME).status_code,
            c.post("/api/v1/contracts/nope/questions", json=q, headers=SAME).status_code,
            c.post(ASK, json=q, headers=CROSS).status_code,
            c.post(
                ASK, json=q, headers={**AS_OPERATOR, "Sec-Fetch-Site": "same-origin"}
            ).status_code,
        ]
        none_yet = c.get("/investigations", headers=AUTH).json()
        asked = c.post(ASK, json=q, headers=SAME).json()
        qid = asked["id"]
        wait_for(c, qid, "done")
        done = c.get(f"/api/v1/investigations/{qid}", headers=VIEW).json()
        more = f"/api/v1/investigations/{qid}/questions"
        cross = c.post(more, json={"question": "and now?"}, headers=CROSS).status_code
        blank = c.post(more, json={"question": ""}, headers=SAME).status_code
        followed = c.post(more, json={"question": "and now?"}, headers=SAME).json()
        wait_for(c, qid, "done")
        after = c.get(f"/api/v1/investigations/{qid}", headers=VIEW).json()
        unknown = c.post(
            "/api/v1/investigations/nope/questions", json={"question": "x"}, headers=SAME
        )
    assert refused == [422, 422, 404, 403, 401] and none_yet == []
    assert (asked["kind"], asked["contract"], asked["title"]) == (
        "question",
        "acme",
        "is checkout healthy?",
    )
    assert done["answer"]["answer"] == ANSWER_1["answer"] and done["can_follow_up"] is True
    assert (done["asked"], done["exchange"]) == ("is checkout healthy?", [])
    assert (cross, blank) == (403, 422)
    assert followed["id"] == qid
    assert after["answer"]["answer"] == ANSWER_2["answer"]
    assert after["asked"] == "and now?"  # the Answer shown answers the follow-up
    (earlier,) = after["exchange"]  # one turn, though a blank follow-up was refused
    assert (earlier["question"], earlier["at"]) == ("is checkout healthy?", done["started"])
    assert earlier["answer"]["answer"] == ANSWER_1["answer"]
    assert unknown.status_code == 404
    assert slack.calls == []  # a console Question has no Slack thread


def test_only_a_question_no_longer_answering_takes_a_follow_up(
    settings: Settings, fake_cluster: Any, database_url: str
) -> None:
    settings.database_url = SecretStr(database_url)
    settings.console_operators = {OPERATOR}
    with client(settings, fake_cluster, call("TriageReport", "r1", **REPORT)) as c:
        tid = c.post(URL, json=ALERT, headers=HOOK).json()["investigation"]
        wait_for(c, tid, "done")
        page = c.get(f"/api/v1/investigations/{tid}", headers=VIEW).json()
        r = c.post(
            f"/api/v1/investigations/{tid}/questions", json={"question": "why?"}, headers=SAME
        )

        async def answering() -> None:  # seeded once the Hub is up: it isn't resumed
            async with connect(database_url) as pool:
                q = Investigation(
                    id="q1", kind="question", contract_id="acme", title="q", started=1
                )
                await Store(pool).insert(q)

        asyncio.run(answering())
        running = c.get("/api/v1/investigations/q1", headers=VIEW).json()
        early = c.post(
            "/api/v1/investigations/q1/questions", json={"question": "and?"}, headers=SAME
        )
    assert page["can_follow_up"] is False and page["asked"] is None and page["exchange"] == []
    assert r.status_code == 409 and r.json()["detail"] == "still answering, or not a Question"
    assert running["status"] == "running" and running["can_follow_up"] is False
    assert early.status_code == 409


def test_the_overview_shows_every_contract_with_its_own_spend(
    settings: Settings, fake_cluster: Any, database_url: str
) -> None:
    settings.database_url = SecretStr(database_url)
    settings.console_operators = {OPERATOR}
    settings.model_prices = {settings.model: HAIKU}  # a budget needs its model priced
    bank = make_contract(
        contract_id="bank",
        webhook_token="bank-token",
        clusters={"east": ["pay"], "west": ["pay"]},
        slack={"operator_channel": "C0BANK000"},
        model=settings.model,
        monthly_budget_usd=20,
    )
    with client(settings, fake_cluster, more={"bank": (bank, fake_cluster)}) as c:

        async def spend() -> None:
            async with connect(database_url) as pool:
                await Store(pool).record_usage(None, "bank", "m", 1, 1, 0.25)

        asyncio.run(spend())
        shown = c.get("/api/v1/overview", headers=VIEW).json()
        theirs_asked = c.post(
            "/api/v1/contracts/bank/questions", json={"question": "is pay healthy?"}, headers=SAME
        ).json()
        no_cookie = c.get("/api/v1/overview", headers=AS_OPERATOR).status_code
    acme, theirs = shown
    assert (acme["id"], acme["clusters"], acme["operator_channel"], acme["model"]) == (
        "acme",
        ["test"],
        None,
        None,
    )
    assert (acme["spent_usd"], acme["budget_usd"]) == (0, None)
    assert (theirs["id"], theirs["clusters"], theirs["operator_channel"]) == (
        "bank",
        ["east", "west"],
        "C0BANK000",
    )
    assert (theirs["model"], theirs["spent_usd"], theirs["budget_usd"]) == (
        settings.model,
        0.25,
        20,
    )
    assert no_cookie == 401
    assert theirs_asked["contract"] == "bank"  # asked through bank's path: bank's Question


def test_a_long_question_is_shown_whole_and_its_title_cut(
    settings: Settings, fake_cluster: Any, database_url: str
) -> None:
    """Audit A2: the Question is stored whole as its title; the console's heading and
    list cut it at 200, what was asked is shown whole."""
    settings.database_url = SecretStr(database_url)
    settings.console_operators = {OPERATOR}
    question = "why " + "x" * 300 + "?"
    with client(settings, fake_cluster, call("Answer", "a1", **ANSWER_1)) as c:
        asked = c.post(ASK, json={"question": question}, headers=SAME).json()
        wait_for(c, asked["id"], "done")
        page = c.get(f"/api/v1/investigations/{asked['id']}", headers=VIEW).json()
    assert asked["title"] == page["title"] == question[:200]
    assert page["asked"] == question


def test_questions_stored_cut_get_their_whole_title_back(database_url: str) -> None:
    """Migration 0043 (audit A2): a Question's title from its input; a Triage's kept."""
    long = "why " + "x" * 300 + "?"
    cut = Investigation(
        id="q-cut", kind="question", contract_id="acme", title=long[:200], started=1.0
    )
    cut.input = {"question": long}
    triage = Investigation(
        id="t-1", kind="triage", contract_id="acme", title="KubePodCrashLooping shop", started=1.0
    )
    triage.input = {"alerts": []}

    async def go() -> dict[str, str]:
        async with connect(database_url) as pool:
            st = Store(pool)
            await st.migrate()
            await st.insert(cut)
            await st.insert(triage)
            async with pool.connection() as conn:
                await conn.execute(
                    "delete from schema_migrations where version = '0043_question_titles'"
                )
            await st.migrate()
            return {i: (await st.get(i)).title for i in ("q-cut", "t-1")}  # type: ignore[union-attr]

    assert asyncio.run(go()) == {"q-cut": long, "t-1": "KubePodCrashLooping shop"}
