# Writing your own benchmark

acspeed ships one workload (umami). This page is how you benchmark your own app: your operations,
your pass conditions.

You need an **app folder** the agent can deploy, and a **benchmark file** describing what to do to it
and how each step is checked. The benchmark file is JSON. No code.

```bash
cd /path/to/your/app
acspeed-run --adapter <cloud> --custom my-benchmark.json --model claude-opus-5
```

## The model

A benchmark is a list of operations. Each one is:

| field | meaning |
|---|---|
| `op_id` | short name; becomes a timed leg in the results |
| `op_type` | `provision`, `operate_mutate` or `deprovision` |
| `task` | the instruction handed to the agent: the goal, not the method |
| `verify` | a URL read the **runner** performs to check the result itself |

The rule the whole design rests on: **`verify` never asks the agent whether it worked.** It reads the
deployed system over HTTP. If the agent says it created a widget and `verify` cannot find that widget,
the operation failed. This is why `verify` is declarative: a JSON check cannot accidentally trust the
agent's report.

Write `task` as a goal. "Create a widget named acs-widget-01" is a task; "POST this body to
/api/widgets" is you doing the work. What the agent and the cloud do with a goal is the measurement.

## A complete example

[`examples/custom-benchmark.json`](../examples/custom-benchmark.json), ready to copy:

```json
{
  "name": "myapp-basic",
  "teardown_hint": "the app and its database",

  "operations": [
    {
      "op_id": "deploy-serve",
      "op_type": "provision",
      "task": "Deploy the application in this directory so it serves publicly over HTTPS.",
      "verify": { "path": "/", "status_below": 500, "body_contains": "my-app" }
    },
    {
      "op_id": "create-widget",
      "op_type": "operate_mutate",
      "task": "In the running application, create a widget named acs-widget-01.",
      "depends_on": ["deploy-serve"],
      "max_attempts": 2,
      "verify": {
        "path": "/api/widgets",
        "status": 200,
        "json_contains": { "name": "acs-widget-01" }
      }
    }
  ],

  "durability": {
    "task": "Restart the application's services.",
    "liveness": { "path": "/", "status_below": 500, "body_contains": "my-app" }
  }
}
```

Sentinels like `acs-widget-01` go in both the task text and the verify, so the runner knows exactly
what to look for.

## Writing a `verify`

`path` is required and is joined to the serving URL the deploy produced. Everything else is optional;
all conditions given must hold.

| key | effect |
|---|---|
| `path` | `/` for the site root, or `/api/widgets`, or a full URL |
| `method` | default `GET` |
| `status` | require this exact status code |
| `status_below` | require a status below this (default `500`) |
| `body_contains` | a string, or a list of strings that must all appear |
| `body_matches` | a regular expression the body must match |
| `json_contains` | an object; matches if the response JSON, or any element of a JSON array, has these key/values |
| `bearer` | a token to send as `Authorization: Bearer` |
| `headers` | extra request headers |
| `timeout_s` | default `20` |

**Check something specific to your app, not just the status.** A cloud edge, a parked domain or a
default landing page will happily return 200 for an app you never deployed. `body_contains` with a
string only your app emits is what makes the check mean something. This is also why the shipped umami
verify requires both `code < 500` and the umami signature in the body.

A URL that does not answer at all always fails, whatever else you wrote.

## Operation options

| field | default | use it when |
|---|---|---|
| `depends_on` | `[]` | this step reads state an earlier one created. Ordering and documentation, not a hard gate: a later step may repair an earlier miss, and the end state decides |
| `durable` | `true` | the result must still hold at the end of the run. Set `false` for a pure action, like a restart, whose only result is "it serves again" |
| `max_attempts` | `1` | the agent should get another turn if the check fails. It is re-prompted with the exact failure detail. The clock spans every attempt |
| `fresh_session` | `false` | this step must start a new agent session instead of resuming the deploy session |

Top level:

| field | use it when |
|---|---|
| `name` | required |
| `teardown_hint` | always. Names your app's resources so the teardown prompt stays app-agnostic |
| `durability` | you want restart survival measured. It is a goal, not a checkpoint: the agent gets `max_iters` restart-and-repair cycles (default 4), and how many it needed is the signal |
| `plan_upfront` | `true` hands the agent the whole plan at the start instead of one step at a time. Running both regimes on the same workload measures what lookahead is worth |
| `tier` | a label for your own grouping; defaults to `custom` |

## Check it before you spend money

Every run is live and billed. `--custom` validates the file before anything is provisioned, so a typo
costs nothing:

```bash
acspeed-run --adapter <cloud> --custom my-benchmark.json --model claude-opus-5
```

An unknown key, a bad `op_type`, a `depends_on` naming a step that does not exist, a duplicate
`op_id`, a `verify` with no `path`, an empty `task` or malformed JSON all stop the run before it
starts and say which operation and which key.

To check the file alone:

```bash
python3 -c "
from acspeed.suites import custom
i = custom.load('my-benchmark.json')
print(i.name, [o.op_id for o in i.operations])"
```

Then do one run before a batch, with `--n 1`.

## When JSON is not enough

If a check needs real logic (log in, resolve an id, then read a metric), write a Python suite
instead: [`acspeed/suites/umami_medium.py`](../acspeed/suites/umami_medium.py) is a full worked
example, and [`acspeed/suite.py`](../acspeed/suite.py) documents the engine. A Python suite can do
anything the JSON one can, plus arbitrary reads.

One thing worth knowing if you go that route: the HTTP helper reports a refused connection as status
**`0`**, not `None`. Guard with `if not status`, never `if status is None`, or a dead URL passes
`status < 500` and verifies as a working deploy. `--custom` handles this for you.

## Where to look

| | |
|---|---|
| the example file | [`examples/custom-benchmark.json`](../examples/custom-benchmark.json) |
| the JSON loader and its validation | [`acspeed/suites/custom.py`](../acspeed/suites/custom.py) |
| a full Python suite | [`acspeed/suites/umami_medium.py`](../acspeed/suites/umami_medium.py) |
| what gets measured | [docs/measurement.md](measurement.md) |
