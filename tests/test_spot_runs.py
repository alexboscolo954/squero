"""CronJobs on spot, the Actuator's side (slice 16.3b): a listed CronJob's run's pods prefer
the spot nodes on the run's first tries and are required off them once the run has a
failed pod; a CronJob whose Jobs allow no retry, or don't count an interrupted pod as
failed, is skipped and said so; its report says it steers runs and which it skips."""

import copy
import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import httpx
import jsonpatch
from kubernetes.client.exceptions import ApiException

from squero import actuator
from squero.actuator import CAPACITY, DELETION_COST, REQUIRED, Desired, Spotted, Spotting
from tests.test_actuator import owned_by, run_of
from tests.test_blockers import an_actuator as an_actuator_of_blockers

LABEL = "karpenter.sh/capacity-type=spot"
TAINT = "karpenter.sh/capacity-type=spot:NoSchedule"
OFF = {"key": "karpenter.sh/capacity-type", "operator": "NotIn", "values": ["spot"]}
PREFERRED = "preferredDuringSchedulingIgnoredDuringExecution"


def runs(*cronjobs: str, taint: str | None = None) -> Spotting:
    return Spotting(label=LABEL, taint=taint, workloads=[], nodes=[], cronjobs=list(cronjobs))


def an_actuator(spot: Spotting | None = None, failed: int | None = 0, uncounted: Any = None) -> Any:
    http = httpx.Client(
        base_url="http://hub", transport=httpx.MockTransport(lambda r: httpx.Response(204))
    )
    a = actuator.Actuator(http, MagicMock(), MagicMock(), "squero-actuator")
    a.spot = spot
    status = SimpleNamespace(failed=failed, uncounted_terminated_pods=uncounted)
    a.apps.read_namespaced_job.return_value = SimpleNamespace(status=status)
    return a


def cronjob(backoff: int | None = 6, *rules: tuple[str, str], per_index: Any = None) -> Any:
    """A CronJob as the batch API reads it, with podFailurePolicy rules (action, condition)."""
    policy = SimpleNamespace(rules=[
        SimpleNamespace(action=action, on_pod_conditions=[SimpleNamespace(type=cond)])
        for action, cond in rules
    ]) if rules else None  # fmt: skip
    spec = SimpleNamespace(
        backoff_limit=backoff, pod_failure_policy=policy, backoff_limit_per_index=per_index
    )
    return SimpleNamespace(spec=SimpleNamespace(job_template=SimpleNamespace(spec=spec)))


def run_pod(cron: str = "nightly") -> dict[str, Any]:
    return owned_by("Job", run_of(cron))


def admitted(a: Any, review: dict[str, Any] | None = None) -> dict[str, Any]:
    req = (review or run_pod())["request"]
    ops = actuator.mutation(req, {}, "squero-actuator", steering=a.steer_pod)
    return jsonpatch.apply_patch(copy.deepcopy(req["object"]), ops)  # type: ignore[no-any-return]


def checked(a: Any, **codes: str | None) -> Any:
    a.spot_checked = {f"shop/{k}": v for k, v in codes.items()}
    return a


# --- the webhook ------------------------------------------------------------------------


def test_a_runs_first_tries_prefer_the_spot_nodes() -> None:
    a = checked(an_actuator(runs("shop/nightly", taint=TAINT)), nightly=None)
    pod = admitted(a)
    assert pod["metadata"]["labels"][CAPACITY] == "spot"
    assert pod["spec"]["affinity"]["nodeAffinity"][PREFERRED][0]["weight"] == 100
    assert pod["spec"]["tolerations"][0]["value"] == "spot"
    assert DELETION_COST not in (pod["metadata"].get("annotations") or {})  # a Job's: none
    a.apps.read_namespaced_job.assert_called_with(
        run_of("nightly"), "shop", _request_timeout=actuator.SPOT_BUDGET
    )


def test_once_the_run_has_a_failed_pod_its_retry_is_required_off_spot() -> None:
    for a in (
        an_actuator(runs("shop/nightly"), failed=1),
        an_actuator(runs("shop/nightly"), failed=None, uncounted=SimpleNamespace(failed=["u1"])),
    ):
        pod = admitted(checked(a, nightly=None))
        assert pod["metadata"]["labels"][CAPACITY] == "on-demand"
        assert pod["spec"]["affinity"]["nodeAffinity"][REQUIRED]["nodeSelectorTerms"] == [
            {"matchExpressions": [OFF]}
        ]
        assert DELETION_COST not in (pod["metadata"].get("annotations") or {})


def test_a_run_is_left_alone_unless_its_cronjob_is_listed_read_and_retries() -> None:
    plain = run_pod()["request"]["object"]
    assert admitted(checked(an_actuator(runs("shop/other")), nightly=None)) == plain
    assert admitted(an_actuator(runs("shop/nightly"))) == plain  # not read yet
    assert admitted(checked(an_actuator(runs("shop/nightly")), nightly="no_retry")) == plain
    assert admitted(checked(an_actuator(None), nightly=None)) == plain
    a = checked(an_actuator(runs("shop/nightly")), nightly=None)
    a.apps.read_namespaced_job.side_effect = ApiException(status=500)
    assert admitted(a) == plain  # its Job not read: nothing placed
    review = run_pod()
    review["request"]["object"]["spec"]["nodeSelector"] = {"karpenter.sh/capacity-type": "spot"}
    assert (
        admitted(checked(an_actuator(runs("shop/nightly")), nightly=None), review)
        == (review["request"]["object"])
    )


# --- the minute's check -----------------------------------------------------------------


def events(a: Any) -> list[tuple[str, str, str]]:
    out = [(c.args[1].involved_object.name, c.args[1].reason, c.args[1].involved_object.api_version)
           for c in a.core.create_namespaced_event.call_args_list]  # fmt: skip
    a.core.create_namespaced_event.reset_mock()
    return out


def test_each_listed_cronjob_is_read_for_a_retry_it_can_tell() -> None:
    a = an_actuator(runs("shop/a", "shop/b", "shop/c", "shop/d", "shop/e", "shop/f", "shop/g"))
    jobs = {
        "a": cronjob(6),
        "b": cronjob(0),  # no retry
        "c": cronjob(6, ("FailJob", "DisruptionTarget")),  # an interruption ends the run
        "d": cronjob(6, ("Ignore", "DisruptionTarget")),  # never counted as failed
        "e": cronjob(None, ("Ignore", "ConfigIssue"), per_index=2),  # unset: 6; another one
        "f": cronjob(6, per_index=0),  # an Indexed Job's index: no retry
        "g": cronjob(6, ("FailIndex", "DisruptionTarget"), per_index=3),  # its index ends
    }
    a.apps.read_namespaced_cron_job.side_effect = lambda name, ns: jobs[name]
    a.check_runs()
    assert a.spot_checked == {
        "shop/a": None,
        "shop/b": "no_retry",
        "shop/c": "no_retry",
        "shop/d": "ignores_disruptions",
        "shop/e": None,
        "shop/f": "no_retry",
        "shop/g": "no_retry",
    }
    # first read: steered or skipped, each said once, a skipped one never said placed
    assert [(e[0], e[1]) for e in events(a)] == [
        ("a", "SpotPlaced"),
        ("b", "SpotSkipped"),
        ("c", "SpotSkipped"),
        ("d", "SpotSkipped"),
        ("e", "SpotPlaced"),
        ("f", "SpotSkipped"),
        ("g", "SpotSkipped"),
    ]
    a.check_runs()
    assert events(a) == []  # said once
    jobs["a"] = cronjob(0)
    a.apps.read_namespaced_cron_job.side_effect = lambda name, ns: (
        (_ for _ in ()).throw(ApiException(status=403)) if name == "b" else jobs[name]
    )
    a.check_runs()
    assert a.spot_checked["shop/a"] == "no_retry" and a.spot_checked["shop/b"] == "no_retry"
    assert events(a) == [("a", "SpotSkipped", "batch/v1")]
    jobs["a"] = cronjob(6)  # retries again: steered, and said so
    a.check_runs()
    assert events(a) == [("a", "SpotPlaced", "batch/v1")]


def test_a_cronjob_joining_or_leaving_the_list_is_said_on_it() -> None:
    a = an_actuator()
    a.steer(runs("shop/nightly"))
    a.say_placed()
    assert events(a) == []  # said by the minute's read, once it knows it isn't skipped
    a.apps.read_namespaced_cron_job.return_value = cronjob(6)
    a.check_runs()
    assert events(a) == [("nightly", "SpotPlaced", "batch/v1")]
    a.steer(Spotting(label=LABEL, cronjobs=None))  # not known now: kept
    assert a.spot and a.spot.cronjobs == ["shop/nightly"]
    a.steer(runs())
    a.say_placed()
    assert events(a) == [("nightly", "SpotEnded", "batch/v1")] and a.spot_checked == {}


def test_a_listed_cronjobs_pause_is_read_and_its_runs_reach_the_steering() -> None:
    a = an_actuator()
    a.apps.list_namespaced_cron_job.return_value = SimpleNamespace(items=[])
    a.apps.read_namespaced_cron_job.return_value = cronjob(6)
    a.pull = lambda: Desired(kill_switch=False, sizings=[], spot=runs("shop/nightly"))  # type: ignore[method-assign]
    a.step()
    a.apps.list_namespaced_cron_job.assert_called_with("shop")  # its pause read
    assert a.spot_checked == {"shop/nightly": None}  # read in the minute
    assert admitted(a)["metadata"]["labels"][CAPACITY] == "spot"


def test_its_report_says_it_steers_runs_and_which_it_skips() -> None:
    seen: list[httpx.Request] = []
    a = an_actuator_of_blockers([204, 204, 204, 204], seen)
    a.blocked(["shop"], 0.0)
    a.spot = Spotting(label=LABEL, workloads=[], cronjobs=None)  # its runs not known yet
    a.blocked(["shop"], 10_000.0)
    a.spot = runs("shop/nightly", "shop/other")
    a.spot_checked = {"shop/nightly": "no_retry", "shop/other": None, "shop/gone": "no_retry"}
    a.blocked(["shop"], 20_000.0)
    a.killed = True
    a.blocked(["shop"], 30_000.0)
    bodies = [json.loads(r.content) for r in seen]
    assert [b.get("spot_runs", False) for b in bodies] == [False, False, True, False]
    assert bodies[2]["spot_skipped"] == {"shop/nightly": "no_retry"}  # listed ones only


def test_the_webhook_over_tls_reaches_the_steering_with_cronjobs_alone() -> None:
    import base64
    import ssl

    cert, key = actuator.certificate("localhost")
    a = checked(an_actuator(runs("shop/nightly")), nightly=None)
    server = actuator.serve(a, cert, key, port=0)
    try:
        trusted = ssl.create_default_context(cadata=cert.decode())
        with httpx.Client(verify=trusted) as http:
            got = http.post(f"https://localhost:{server.server_address[1]}/mutate", json=run_pod())
    finally:
        server.shutdown()
    ops = json.loads(base64.b64decode(got.json()["response"]["patch"]))
    assert any(op["path"].endswith(CAPACITY.replace("/", "~1")) for op in ops)


def test_a_change_its_report_tells_brings_the_report_forward() -> None:
    """The report is hourly: the Hub hears that it steers, or a new skip, now."""
    a = an_actuator()
    a.blockers_due = 10_000.0
    a.steer(runs("shop/nightly"))  # it steers runs now
    assert a.blockers_due == 0.0
    a.blockers_due = 10_000.0
    a.steer(runs("shop/nightly", "shop/other"))  # the same it says: no hurry
    assert a.blockers_due == 10_000.0
    a.apps.read_namespaced_cron_job.return_value = cronjob(6)
    a.check_runs()  # read for the first time: what it reports of them changes
    assert a.blockers_due == 0.0
    a.blockers_due = 10_000.0
    a.check_runs()
    assert a.blockers_due == 10_000.0  # nothing new
    a.apps.read_namespaced_cron_job.return_value = cronjob(0)
    a.check_runs()
    assert a.blockers_due == 0.0


def test_a_count_that_fails_leaves_no_cronjob_unread() -> None:
    a = an_actuator()
    a.apps.read_namespaced_cron_job.return_value = cronjob(6)
    a.core.list_namespaced_pod.side_effect = RuntimeError("its bug")
    web = Spotted(namespace="shop", kind="Deployment", name="web", ratio=0.7, on_demand=2)
    both = runs("shop/nightly").model_copy(update={"workloads": [web]})  # a count to fail
    a.pull = lambda: Desired(kill_switch=False, sizings=[], spot=both)  # type: ignore[method-assign]
    a.step()
    assert a.spot_checked == {"shop/nightly": None}


def test_the_skip_codes_are_the_same_everywhere() -> None:
    """A code the Hub doesn't know would have it refuse the whole report."""
    from squero import blockers, sizing

    assert set(actuator.SPOT_SKIPS) == set(sizing.SKIPS)
    for code in actuator.SPOT_SKIPS:
        blockers.Report.model_validate(
            {"at": 0, "namespaces": [], "pods": [], "spot_skipped": {"shop/a": code}}
        )
