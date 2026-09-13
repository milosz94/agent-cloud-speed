# Writing your own benchmark

acspeed ships one workload (umami, Easy and Medium tiers). This page is how you add your own: your
app, your operations, your pass conditions.

You need two things: an **app folder** the agent can deploy, and a **suite** describing the
operations to perform on it and how to check each one.

## The model

A benchmark is a list of operations. Each operation is:

| field | meaning |
|---|---|
| `op_id` | short identifier, appears in results as a timed leg |
| `op_type` | `provision`, `operate_mutate` or `deprovision` |
| `task` | the instruction handed to the agent, stating the goal and not the method |
| `verify` | a function the **runner** calls to read the postcondition back itself |

The one rule that matters: **`verify` must never trust the agent's report.** It reads the deployed
system directly, over HTTP or the app's API. If the agent says it registered a user and `verify`
cannot find that user, the operation failed. An agent grading its own homework measures nothing.

Write `task` as a goal, not a recipe. "Register a user named X and confirm it persists" is a task;
"run `POST /api/users` with this body" is you doing the work. The point of the benchmark is what the
agent and the cloud do with a goal.

## A minimal suite

Create `acspeed/suites/myapp.py`:

```python
from ..suite import TierInstance, TierOperation, OpContext, VerifyResult, DurabilityGoal
from ._http import http

WIDGET = "acs-widget-01"


def _serves(ctx: OpContext) -> VerifyResult:
    """The deploy worked if the app itself answers, and answers as YOUR app."""
    status, body, _json = http("GET", ctx.url)
    if not status:                       # 0 = curl could not connect at all (see the trap below)
        return VerifyResult(ok=False, detail="nothing listening")
    served = status < 500 and "my-app" in body.lower()
    return VerifyResult(ok=served, detail=f"HTTP {status}, signature={'yes' if served else 'no'}",
                        measured=status)


def _has_widget(ctx: OpContext) -> VerifyResult:
    """Read the postcondition back from the app, not from what the agent claimed."""
    status, body, _json = http("GET", f"{ctx.url}/api/widgets")
    if not status or status >= 500:
        # The INSTRUMENT could not read, which is not the same as the cloud failing. Say so, or you
        # score the cloud down for your own blind spot.
        return VerifyResult(ok=False, unverifiable=True, detail=f"widget API unreadable ({status})")
    return VerifyResult(ok=WIDGET in body, detail=f"looked for {WIDGET} in {len(body)} bytes")


def build(**kwargs) -> TierInstance:
    return TierInstance(
        name="myapp-basic",
        tier="medium",
        teardown_hint="the app and its database",
        operations=[
            TierOperation(
                op_id="deploy-serve",
                op_type="provision",
                task="Deploy the application in this directory so it serves publicly over HTTPS.",
                verify=_serves,
            ),
            TierOperation(
                op_id="create-widget",
                op_type="operate_mutate",
                task=f"Create a widget named {WIDGET} in the running application.",
                verify=_has_widget,
                depends_on=("deploy-serve",),
            ),
        ],
        durability=DurabilityGoal(
            task="Restart the application's services.",
            liveness=_serves,
        ),
    )
```

`http()` returns `(status, body_text, parsed_json_or_none)` and never raises. The sentinel `WIDGET` goes into both the task text and the verify, so the runner
knows exactly what to look for. Anything discovered at runtime (an id the verify had to look up)
belongs in `ctx.state`.

## Register and run it

Add it to `_REGISTRY` in [`acspeed/suites/__init__.py`](../acspeed/suites/__init__.py):

```python
from . import myapp

_REGISTRY = {
    ...
    "myapp-basic": myapp.build,
}
```

Then:

```bash
cd /path/to/your/app
acspeed-run --adapter <cloud> --suite myapp-basic --model claude-opus-5
```

Each operation becomes its own timed leg in `tables.txt`, alongside the deploy and each durability
cycle.

## The options you will actually need

| field | default | use it when |
|---|---|---|
| `depends_on` | `()` | this operation reads state an earlier one created. Ordering and documentation, not a hard gate |
| `durable` | `True` | the postcondition must still hold at the end of the run. Set `False` for a pure action, like a restart, whose only postcondition is "it serves again" |
| `max_attempts` | `1` | the agent should get another turn on a failed verify. It is re-prompted with the exact failure detail. The operation's clock spans every attempt |
| `fresh_session` | `False` | this operation must start a new agent session instead of resuming the deploy session |
| `harness_action` | `None` | the **runner** performs this operation and no agent turn is taken. For work the harness must own, such as generating load from outside the cloud |

On the instance:

| field | use it when |
|---|---|
| `teardown_hint` | always. Names your app's resources so the teardown prompt stays app-agnostic |
| `durability` | you want restart survival measured. It is a goal, not a checkpoint: the agent gets `max_iters` restart-and-repair cycles, and how many it needed is the signal |
| `plan_upfront` | `True` hands the agent the whole plan at the start instead of one operation at a time. Running both regimes on the same workload measures what lookahead is worth |

## The trap: status 0, and checking status alone

`http()` reports a connection failure as status **`0`**, not `None` (`None` only happens if curl
itself cannot be run). So `if status is None` does not catch a dead URL, and `status < 500` is
**true for 0**. A verify written that way passes a deployment that does not exist.

Always guard with `if not status`, and check a signature in the body rather than the status alone.
The shipped umami verify does exactly this: `ok = code < 500 and is_umami`. A cloud edge or a parked
domain will happily return 200 for an app you never deployed.

## `unverifiable` is not failure

If your verify cannot perform its read, set `unverifiable=True` rather than `ok=False`. That says
the instrument failed, not the cloud.

This is not a formality. Across 96 integrate checks in the published tree, 7 failures were caused by
the harness (lost beacons, no headless browser, an unrecognised response shape) against 4 genuine
agent failures. In one cell every single failure was instrument-caused, and scoring those as failures
blamed the cloud for the harness's blind spot.

## Before you spend money on it

Every run is live and billed. Check the suite loads and its shape is valid first:

```bash
python3 -c "
from acspeed.suites import get_instance
s = get_instance('myapp-basic')
print(s.name, [o.op_id for o in s.operations])"
```

`TierOperation` validates `op_type` and `verify` on construction, so a typo fails here rather than
after the agent is running.

Then do one run before a batch:

```bash
acspeed-run --adapter <cloud> --suite myapp-basic --model claude-opus-5 --n 1
```

## Where to look

| | |
|---|---|
| the engine and its contract | [`acspeed/suite.py`](../acspeed/suite.py) |
| a full worked suite | [`acspeed/suites/umami_medium.py`](../acspeed/suites/umami_medium.py) |
| the HTTP helper | [`acspeed/suites/_http.py`](../acspeed/suites/_http.py) |
| what gets measured | [docs/measurement.md](measurement.md) |
