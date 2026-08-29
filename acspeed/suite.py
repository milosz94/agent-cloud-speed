"""The benchmark suite engine (Easy / Medium / Hard): the live execution layer that drives a tier's
operations against a real deployment.

This module adds NOTHING to the measurement or scoring math. It sits ABOVE them:

  * ``operation.py`` defines what an operation IS (PROVISION / OPERATE_MUTATE / DEPROVISION) and states
    that observe/read is NOT a fourth type, it is the primitive used to confirm state inside every
    operation's lifecycle (Part 2, Section 6.5).
  * ``reference.py`` turns a trajectory of operations into ``Edge``s of a ``ReferenceGraph`` and scores
    selection-vs-execution excess (Part 3).

What did not exist is the thing that RUNS a tier live: hands the agent each operation as a task,
lets the agent own the METHOD, and then calls the observe primitive to VERIFY that operation's
postcondition independently. That is this engine. Its output (per-operation pass/fail + the agent
turns' transcripts) feeds the existing measurement (``measure()``) and scoring (``reference.py``).

Design contract, agreed with the design owner and consistent with the paper:

  1. An operation is a TYPE (abstract) instantiated by an INSTANCE (concrete). ``mutate`` is a slot;
     "register a user" is one instance of it. ``integrate(A->B)`` is a slot; "wire umami into site B"
     is one instance. Swapping the instance changes the app under test, not the engine.
  2. The agent owns the METHOD. How state gets created depends on the surface the agent chose (SSH to
     a raw VM, exec into a container, the app's own API on serverless). No method is right or wrong and
     none is scored directly; only the verified RESULT is. The cost of the path the agent took is
     priced by the efficiency axis (``reference.py``), never as a pass/fail on the method.
  3. The runner owns VERIFICATION only. Every postcondition is read back by US (the observe primitive),
     never taken from the agent's self-report. A verify predicate that cannot fail is not a control, so
     each instance's predicates must be validated against a live app before they are trusted.
  4. Success is the CONJUNCTION of every durable postcondition, RE-VERIFIED at the terminal state.
     Because the agent may restructure the whole deployment (redeploy to satisfy a blocked operation),
     an operation that passed mid-run can be broken by a later one (a redeploy wipes the DB, the
     registered user is gone). So a checkpoint that passed once is not trusted: at the end, after the
     last restart / fault, the runner re-reads every durable sentinel and they must all still hold.
     This is why the restructure freedom and the durability check are the same idea, and why CP7
     (restart-durability) is just this general rule with a restart placed before the terminal re-verify.

The engine takes the agent executor as an injected callable so it is fully unit-testable without a
live agent or a live app: a fake executor + toggleable verify predicates exercise sequencing, the
resume-session wiring, and the terminal conjunction (including a durable sentinel LOST after a
restart). The only piece that needs a live app is an instance's own verify predicates.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence

from .operation import DEPROVISION, OPERATE_MUTATE, OPERATION_TYPES, PROVISION

__all__ = [
    "VerifyResult",
    "OpContext",
    "TierOperation",
    "OpOutcome",
    "TierInstance",
    "TierRun",
    "run_tier",
]


@dataclass(frozen=True)
class VerifyResult:
    """The outcome of the runner reading a postcondition back (the observe primitive). ``ok`` is the
    only thing that gates pass/fail; ``detail`` and ``measured`` are for the report and never trusted
    as truth about state on their own."""

    ok: bool
    detail: str = ""
    measured: object = None


@dataclass
class OpContext:
    """Live context threaded through a tier run. ``url`` is the serving URL the deploy established;
    ``state`` is the runner's OWN bookkeeping (sentinel identifiers it chose, handles it discovered
    while verifying, e.g. a website id). Instances read/write ``state`` to carry a sentinel from the
    task text to the verify. Nothing here comes from the agent except the served URL it produced."""

    url: Optional[str] = None
    state: Dict[str, object] = field(default_factory=dict)


# The runner-side verify: given the live context, independently read the postcondition. This IS the
# observe primitive (operation.py, Section 6.5). It must never consult the agent's report.
Verify = Callable[[OpContext], VerifyResult]

# The injected agent executor: run ONE agent turn for this operation and return the agent's JSON
# result (the same shape autorun's ``_claude`` returns: session_id, is_error, total_cost_usd, and,
# for a deploy turn, optionally a resolved ``url``). ``resume_sid`` is the session to continue, or
# None for a fresh turn. The engine never inspects HOW the agent did the work.
RunAgent = Callable[["TierOperation", OpContext, Optional[str]], dict]


@dataclass
class TierOperation:
    """One operation in a tier: an abstract TYPE plus the concrete hooks that instantiate it.

    ``op_type`` is one of ``operation.py``'s types so the trajectory maps straight onto the Part 3
    graph. ``task`` is the method-free instruction handed to the agent. ``verify`` is the runner's
    independent postcondition. ``depends_on`` names operations that must appear earlier (ordering /
    documentation, not a hard skip: the agent's restructure freedom means a later op may repair an
    earlier miss, and the terminal conjunction is what decides the truth). ``durable`` marks an
    operation whose postcondition must STILL hold at the terminal state (identity, recorded state);
    a pure action with no state of its own (a restart, whose only postcondition is "serves again")
    is ``durable=False``. ``fresh_session`` forces a new agent turn instead of resuming the deploy
    session (default is to resume, so the deployment context persists across operations)."""

    op_id: str
    op_type: str
    task: str
    verify: Verify
    depends_on: Sequence[str] = ()
    durable: bool = True
    fresh_session: bool = False

    def __post_init__(self) -> None:
        if self.op_type not in OPERATION_TYPES:
            raise ValueError(
                f"op_type {self.op_type!r} for operation {self.op_id!r} is not one of {OPERATION_TYPES}"
            )
        if not callable(self.verify):
            raise ValueError(f"operation {self.op_id!r} has no callable verify predicate")


@dataclass
class OpOutcome:
    """What happened for one operation: the agent turn's result, and the runner's verify at the time
    the operation ran (the mid-run reading, before any later restructure could disturb it)."""

    op: TierOperation
    agent: dict
    verify: VerifyResult

    @property
    def ok(self) -> bool:
        return self.verify.ok


@dataclass
class TierInstance:
    """A concrete benchmark: a named app scenario that instantiates a tier's operation graph. The
    operations run in listed order; every ``depends_on`` must name an operation listed earlier.

    ``teardown_hint`` names the app-specific resources this instance's run creates (e.g. "the app, its
    managed database, and the second website deployed for the integration"), so the harness's teardown
    prompt can stay CLOUD- and APP-agnostic: the orchestrator asks the agent to remove
    ``teardown_hint`` without itself knowing anything about umami or the tier. App specifics live here,
    not in autorun."""

    name: str
    tier: str
    operations: List[TierOperation]
    teardown_hint: str = ""

    def __post_init__(self) -> None:
        if not self.operations:
            raise ValueError(f"tier instance {self.name!r} has no operations")
        seen: set = set()
        for op in self.operations:
            for dep in op.depends_on:
                if dep not in seen:
                    raise ValueError(
                        f"operation {op.op_id!r} depends on {dep!r}, which is not listed before it"
                    )
            if op.op_id in seen:
                raise ValueError(f"duplicate operation id {op.op_id!r} in instance {self.name!r}")
            seen.add(op.op_id)


@dataclass
class TierRun:
    """The result of driving a tier instance: the per-operation outcomes (mid-run), and the terminal
    conjunction (every durable operation's postcondition re-read at the end, after the last restart /
    fault). ``passed`` is the honest gate: every operation verified when it ran AND every durable
    sentinel still holds at the terminal state."""

    instance_name: str
    tier: str
    outcomes: List[OpOutcome]
    terminal: Dict[str, VerifyResult]

    @property
    def passed(self) -> bool:
        if not all(o.ok for o in self.outcomes):
            return False
        return all(v.ok for v in self.terminal.values())

    def failures(self) -> List[str]:
        """The op_ids that failed, split by phase, for the report."""
        bad = [f"{o.op.op_id} (mid-run)" for o in self.outcomes if not o.ok]
        bad += [f"{op_id} (terminal)" for op_id, v in self.terminal.items() if not v.ok]
        return bad

    def summary(self) -> dict:
        return {
            "instance": self.instance_name,
            "tier": self.tier,
            "passed": self.passed,
            "operations": [
                {
                    "op_id": o.op.op_id,
                    "op_type": o.op.op_type,
                    "durable": o.op.durable,
                    "verify_ok": o.verify.ok,
                    "verify_detail": o.verify.detail,
                    "agent_error": bool(o.agent.get("is_error")),
                    "agent_session": o.agent.get("session_id"),
                    "agent_cost_usd": o.agent.get("total_cost_usd"),
                }
                for o in self.outcomes
            ],
            "terminal_reverify": {
                op_id: {"ok": v.ok, "detail": v.detail} for op_id, v in self.terminal.items()
            },
            "failures": self.failures(),
        }


def run_tier(
    instance: TierInstance,
    ctx: OpContext,
    *,
    run_agent: RunAgent,
    log: Callable[[str], None] = lambda _m: None,
) -> TierRun:
    """Drive a tier instance against a live deployment.

    For each operation, in listed order: run ONE agent turn (resuming the deploy session unless the
    operation asks for a fresh one), then have the RUNNER verify the postcondition (the observe
    primitive). The deploy session id is captured from the first operation's turn and reused so the
    agent keeps its deployment context across operations; if a turn returns a URL, it updates the
    context (the served URL the later verifies read against).

    After every operation, re-verify each DURABLE operation's postcondition at the terminal state.
    That is the conjunction the tier's pass/fail is decided on, and it is what makes a sentinel lost
    to a mid-run restructure (or a restart) an honest failure rather than a checkpoint that once
    passed. ``run_agent`` is injected so the engine is testable without a live agent."""
    outcomes: List[OpOutcome] = []
    resume_sid: Optional[str] = None

    for op in instance.operations:
        use_sid = None if op.fresh_session else resume_sid
        log(f"operation {op.op_id} ({op.op_type}): agent turn (resume={use_sid or 'new'})")
        agent = run_agent(op, ctx, use_sid) or {}

        # Capture the session to resume, and the served URL, from whatever the turn produced. The
        # first turn that yields a session id becomes the deployment session the rest resume.
        sid = agent.get("session_id")
        if sid and resume_sid is None:
            resume_sid = sid
        url = agent.get("url")
        if url:
            ctx.url = url

        result = op.verify(ctx)
        log(f"operation {op.op_id}: verify {'OK' if result.ok else 'FAIL'} - {result.detail}")
        outcomes.append(OpOutcome(op=op, agent=agent, verify=result))

    terminal: Dict[str, VerifyResult] = {}
    for op in instance.operations:
        if not op.durable:
            continue
        v = op.verify(ctx)
        log(f"terminal re-verify {op.op_id}: {'OK' if v.ok else 'FAIL'} - {v.detail}")
        terminal[op.op_id] = v

    run = TierRun(
        instance_name=instance.name, tier=instance.tier, outcomes=outcomes, terminal=terminal
    )
    log(
        f"tier {instance.tier} / {instance.name}: "
        f"{'PASS' if run.passed else 'FAIL ' + ', '.join(run.failures())}"
    )
    return run
