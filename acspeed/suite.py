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

import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence

from .operation import DEPROVISION, OPERATE_MUTATE, OPERATION_TYPES, PROVISION

__all__ = [
    "VerifyResult",
    "OpContext",
    "TierOperation",
    "DurabilityGoal",
    "OpOutcome",
    "TierInstance",
    "TierRun",
    "run_tier",
    "full_plan_preamble",
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
    # how many agent turns the runner will give this operation to satisfy its postcondition. On a failed
    # verify (up to this many attempts) the runner re-prompts the SAME session with the exact failure
    # detail, so the agent can adapt (e.g. redeploy an immutable second site whose served page did not yet
    # carry the tracking snippet). Default 1 = one shot. The op's wall spans all attempts (honest cost).
    max_attempts: int = 1

    def __post_init__(self) -> None:
        if self.op_type not in OPERATION_TYPES:
            raise ValueError(
                f"op_type {self.op_type!r} for operation {self.op_id!r} is not one of {OPERATION_TYPES}"
            )
        if not callable(self.verify):
            raise ValueError(f"operation {self.op_id!r} has no callable verify predicate")
        if self.max_attempts < 1:
            raise ValueError(f"operation {self.op_id!r} max_attempts must be >= 1")


@dataclass
class DurabilityGoal:
    """Restart-durability as a GOAL, not a pass/fail checkpoint (design-owner ruling, 2026-08-30).

    The agent restarts its services; if a durable sentinel (the registered user, the recorded pageview)
    did NOT survive the restart, that is not a failure to score, it is work still to do: the agent must
    re-architect (persistent volumes, a managed or persistent datastore, restart policies, or a wholly
    different architecture) and try again, up to ``max_iters`` restart+repair cycles. The tier is
    achieved when EVERY durable postcondition survives a restart; the number of cycles it took is the
    measured signal (a design that is durable on the first restart is more efficient than one that
    needed three re-architectures). Only if the budget is exhausted with a sentinel still lost is the
    run not durable.

    ``task`` is the base restart instruction (the criterion, not the method); on a retry the runner
    appends which specific sentinels were lost so the agent repairs the right thing. ``liveness`` is the
    optional "app serves again after the restart" read (R6), verified each cycle."""

    task: str
    op_id: str = "restart-durability"
    max_iters: int = 4
    liveness: Optional[Verify] = None
    # Optional app hook: after a cycle where some durable sentinel was LOST, re-create the state that
    # only the HARNESS can produce (e.g. re-drive the tracked visit that generates a pageview), so the
    # NEXT restart tests the re-architected storage against freshly-established state. Called with
    # (ctx, lost_op_ids). Without it, state the harness generated (a pageview) cannot be recovered once
    # a restart wipes it, so the tier can only pass if storage was durable from the start.
    reestablish: Optional[Callable[["OpContext", List[str]], None]] = None

    def __post_init__(self) -> None:
        if self.max_iters < 1:
            raise ValueError("durability max_iters must be >= 1")
        if self.liveness is not None and not callable(self.liveness):
            raise ValueError("durability liveness must be callable or None")
        if self.reestablish is not None and not callable(self.reestablish):
            raise ValueError("durability reestablish must be callable or None")


@dataclass
class OpOutcome:
    """What happened for one operation: the agent turn's result, and the runner's verify at the time
    the operation ran (the mid-run reading, before any later restructure could disturb it)."""

    op: TierOperation
    agent: dict
    verify: VerifyResult
    # the operation's wall window (Unix epochs): slot-1 start signal (the agent turn began) and slot-5
    # end signal (its postcondition first verified). autorun slices the session transcript to this
    # window to compute the operation's slot-7 platform/agent split. None when not recorded.
    started_at: Optional[float] = None
    verified_at: Optional[float] = None

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
    durability: Optional[DurabilityGoal] = None
    # Information regime. False (default) = ONLINE (Medium A): operations are revealed one at a time, so the
    # agent cannot overlap work it has not yet seen. True = DISCLOSED (Medium B): the full plan is handed to
    # the agent up front (see full_plan_preamble), so it can schedule with lookahead (overlap independent
    # provisions, choose durable storage from the first step). The paired makespan gap
    # M_online - M_disclosed is the VALUE OF PLAN LOOKAHEAD (Part-3 Sec-5 clairvoyance gap), reported as a
    # tier-level companion; it is NOT the Sec-4 selection excess (M_twin - F_C), which stays reported per run.
    plan_upfront: bool = False
    # Medium has TWO public sites (the primary app and a second site) that both carry this run's token, so a
    # token-only URL picker cannot tell them apart when they come up at once (Medium B provisions them
    # concurrently). The fix is names: the agent is told to name each site '<name>-<token>', and the harness
    # selects the PRIMARY by ``primary_url_name`` (positively, by its own name), so the second site is never
    # mistaken for it. App-specific, so it lives here, not in autorun. Both empty for a single-deployment tier
    # (Easy), where the generic token-only naming applies and there is nothing to disambiguate.
    primary_url_name: str = ""
    second_site_url_name: str = ""

    def __post_init__(self) -> None:
        if not self.operations:
            raise ValueError(f"tier instance {self.name!r} has no operations")
        if self.durability is not None and not any(op.durable for op in self.operations):
            raise ValueError(
                f"tier instance {self.name!r} declares a durability goal but has no durable operations "
                "to survive the restart"
            )
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
    # the restart+repair iterations of the durability goal (report-only; they do not gate pass/fail,
    # the terminal conjunction does). Empty when the instance has no durability goal.
    durability_outcomes: List[OpOutcome] = field(default_factory=list)
    durability_iters: int = 0
    # the iteration at which every durable sentinel first survived a restart, or None if the budget
    # was exhausted with a sentinel still lost.
    durability_achieved: Optional[int] = None

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

    # Part-5 scoring: checkpoint partial credit plus a full-completion bonus, so a run that provisions
    # and wires but misses the recorded-visit or restart still scores, and the break point is located.
    FULL_COMPLETION_BONUS = 0.5

    def score(self) -> dict:
        """Graded score, not just pass/fail. Each operation's postcondition is a checkpoint; the
        restart-durability goal is one more. ``partial_credit`` is the fraction of checkpoints met
        (0..1); a fully-complete run additionally earns ``FULL_COMPLETION_BONUS``, so completing strictly
        beats a high partial. The failing checkpoint(s) are named so the break point is located."""
        checkpoints = [(o.op.op_id, o.ok) for o in self.outcomes]
        ran_durability_goal = self.durability_iters > 0 or self.durability_achieved is not None
        if ran_durability_goal:
            checkpoints.append(("restart-durability", self.durability_achieved is not None))
        else:
            checkpoints.append(("terminal-durability", all(v.ok for v in self.terminal.values())))
        n = len(checkpoints)
        passed = sum(1 for _, ok in checkpoints if ok)
        partial = round(passed / n, 4) if n else 0.0
        completed = self.passed
        return {
            "checkpoints": [{"name": nm, "ok": ok} for nm, ok in checkpoints],
            "passed": passed,
            "total": n,
            "partial_credit": partial,
            "completed": completed,
            "full_completion_bonus": self.FULL_COMPLETION_BONUS if completed else 0.0,
            "score": round(partial + (self.FULL_COMPLETION_BONUS if completed else 0.0), 4),
            "break_point": self.failures(),
        }

    def _op_row(self, o: "OpOutcome") -> dict:
        wall = None
        if o.started_at is not None and o.verified_at is not None:
            wall = round(max(0.0, o.verified_at - o.started_at), 1)
        return {
            "op_id": o.op.op_id,
            "op_type": o.op.op_type,
            "durable": o.op.durable,
            "verify_ok": o.verify.ok,
            "verify_detail": o.verify.detail,
            "agent_error": bool(o.agent.get("is_error")),
            "agent_session": o.agent.get("session_id"),
            "agent_cost_usd": o.agent.get("total_cost_usd"),
            # slot-1 / slot-5 wall window; autorun fills "split" (slot 7) by slicing the transcript.
            "started_at": o.started_at,
            "verified_at": o.verified_at,
            "wall_s": wall,
            "split": None,
        }

    def summary(self) -> dict:
        return {
            "instance": self.instance_name,
            "tier": self.tier,
            "passed": self.passed,
            "score": self.score(),
            "operations": [self._op_row(o) for o in self.outcomes],
            "durability": {
                # restart-durability as a goal: how many restart+repair cycles it took (design ruling)
                "iters": self.durability_iters,
                "achieved_on_iter": self.durability_achieved,
                "achieved": self.durability_achieved is not None,
                "cycles": [self._op_row(o) for o in self.durability_outcomes],
            },
            "terminal_reverify": {
                op_id: {"ok": v.ok, "detail": v.detail} for op_id, v in self.terminal.items()
            },
            "failures": self.failures(),
        }


def full_plan_preamble(instance: TierInstance, token: str = "") -> str:
    """Medium B (DISCLOSED regime): the complete operation plan revealed up front, so the agent can
    schedule with lookahead (overlap independent provisions, pick durable storage from the first step)
    instead of discovering each operation one at a time. Discloses WHAT each operation's goal is, never
    HOW to do it. Prepended to the deploy prompt for a ``plan_upfront`` instance; the online regime
    (Medium A) omits it and reveals operations one at a time."""
    lines = []
    for n, op in enumerate(instance.operations, 1):
        lines.append(f"  {n}. {op.task.strip()}")
    if instance.durability is not None:
        lines.append(f"  {len(instance.operations) + 1}. {instance.durability.task.strip()}")
    plan = "\n".join(lines)
    primary = getattr(instance, "primary_url_name", "") or ""
    second = getattr(instance, "second_site_url_name", "") or ""
    if not token:
        naming = ""
    elif primary and second:
        naming = ("\n\nName the first app's public hostname '" + primary + "-" + token + "' and the second "
                  "site's public hostname '" + second + "-" + token + "' (each site's own name followed by "
                  "this run's token '" + token + "'). Every resource you provision must carry this run's "
                  "token in its public hostname, so this run's resources are uniquely identified and torn "
                  "down cleanly.")
    else:
        naming = ("\n\nEvery resource you provision for this run (the app, its datastore, the second site) "
                  "must carry this run's token '" + token + "' in its public hostname, so this run's "
                  "resources are uniquely identified and torn down cleanly.")
    return (
        "\n\nYOU ARE GIVEN THE COMPLETE PLAN UP FRONT. This deployment will require ALL of the following, "
        "and you know every one of them now (each will be checked afterward):\n\n" + plan +
        "\n\nBecause you know the whole plan now, schedule it freely and efficiently: provision independent "
        "pieces concurrently rather than one after another, and choose durable storage and the right "
        "architecture from the very first step so nothing has to be migrated or rebuilt later. Complete the "
        "ENTIRE plan in this session now; the later steps only confirm each part is in place." + naming
    )


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
        started_at = time.time()
        agent: dict = {}
        result: Optional[VerifyResult] = None
        for attempt in range(1, op.max_attempts + 1):
            use_sid = None if (op.fresh_session and attempt == 1) else resume_sid
            # on a retry, hand the agent the SAME task plus the exact postcondition failure to adapt to.
            run_op = op
            if attempt > 1 and result is not None:
                run_op = TierOperation(
                    op_id=op.op_id, op_type=op.op_type, durable=op.durable,
                    task=(op.task + f"\n\nYOUR PREVIOUS ATTEMPT DID NOT SATISFY THE CHECK: "
                          f"{result.detail}. Fix exactly that and complete the operation."),
                    verify=op.verify)
            log(f"operation {op.op_id} ({op.op_type}): agent turn "
                f"(resume={use_sid or 'new'}, attempt {attempt}/{op.max_attempts})")
            agent = run_agent(run_op, ctx, use_sid) or {}
            sid = agent.get("session_id")
            if sid and resume_sid is None:
                resume_sid = sid
            url = agent.get("url")
            if url:
                ctx.url = url
            result = op.verify(ctx)
            if result.ok:
                if attempt > 1:
                    log(f"operation {op.op_id}: recovered on attempt {attempt}")
                break
            log(f"operation {op.op_id}: verify FAIL (attempt {attempt}/{op.max_attempts}) - {result.detail}")

        verified_at = time.time()
        log(f"operation {op.op_id}: verify {'OK' if result and result.ok else 'FAIL'} - "
            f"{result.detail if result else 'no result'}")
        outcomes.append(OpOutcome(op=op, agent=agent, verify=result or VerifyResult(False, "no attempt"),
                                  started_at=started_at, verified_at=verified_at))

    durable_ops = [op for op in instance.operations if op.durable]
    terminal: Dict[str, VerifyResult] = {}
    dur_outcomes: List[OpOutcome] = []
    dur_iters = 0
    dur_achieved: Optional[int] = None

    if instance.durability is None:
        # No durability goal: a single terminal re-verify of every durable postcondition.
        for op in durable_ops:
            v = op.verify(ctx)
            log(f"terminal re-verify {op.op_id}: {'OK' if v.ok else 'FAIL'} - {v.detail}")
            terminal[op.op_id] = v
    else:
        # Restart-durability as a GOAL: restart, and if any durable sentinel did not survive, hand the
        # agent the list of what was lost and let it re-architect, up to max_iters cycles. The terminal
        # conjunction is the LAST cycle's re-read of every durable postcondition; the run is durable iff
        # they all hold within the budget (dur_achieved set to that cycle).
        g = instance.durability
        lost: List[str] = []
        for it in range(1, g.max_iters + 1):
            dur_iters = it
            task = g.task
            if lost:
                task = (
                    task + "\n\nAFTER YOUR LAST RESTART these did NOT survive: " + ", ".join(lost)
                    + ". The deployment is not durable yet. Change whatever is necessary - persistent "
                    "volumes, a managed or persistent datastore, restart policies, or a different "
                    "architecture - so that ALL of them survive a restart, then restart again to prove it."
                )
            restart_op = TierOperation(
                op_id=g.op_id, op_type=OPERATE_MUTATE, task=task,
                verify=(g.liveness or (lambda _c: VerifyResult(True, "restart turn (no liveness read)"))),
                durable=False,
            )
            log(f"durability cycle {it}/{g.max_iters}: restart + repair turn")
            started_at = time.time()
            agent = run_agent(restart_op, ctx, resume_sid) or {}
            sid = agent.get("session_id")
            if sid and resume_sid is None:
                resume_sid = sid
            url = agent.get("url")
            if url:
                ctx.url = url
            live = restart_op.verify(ctx)  # R6: the app serves again after the restart
            verified_at = time.time()
            results = {op.op_id: op.verify(ctx) for op in durable_ops}
            dur_outcomes.append(OpOutcome(op=restart_op, agent=agent, verify=live,
                                          started_at=started_at, verified_at=verified_at))
            terminal = results
            lost = [op_id for op_id, v in results.items() if not v.ok]
            log(
                f"durability cycle {it}: serves={'yes' if live.ok else 'no'}; "
                + ("all durable state survived the restart" if not lost else f"LOST {lost}")
            )
            if not lost:
                dur_achieved = it
                break
            if g.reestablish is not None and it < g.max_iters:
                # re-create harness-generated state (e.g. the tracked visit) on the new architecture so
                # the next restart tests persistence of freshly-established state, not a lost event.
                try:
                    g.reestablish(ctx, lost)
                    log(f"durability cycle {it}: re-established {lost} before the next restart")
                except Exception as e:  # noqa: BLE001 - reestablish is best-effort
                    log(f"durability cycle {it}: reestablish raised {e!r} (ignored)")
        if dur_achieved is None:
            log(f"durability NOT achieved within {g.max_iters} cycles; still lost: {lost}")

    run = TierRun(
        instance_name=instance.name, tier=instance.tier, outcomes=outcomes, terminal=terminal,
        durability_outcomes=dur_outcomes, durability_iters=dur_iters, durability_achieved=dur_achieved,
    )
    log(
        f"tier {instance.tier} / {instance.name}: "
        f"{'PASS' if run.passed else 'FAIL ' + ', '.join(run.failures())}"
        + (f" (durable on cycle {dur_achieved}/{dur_iters})" if dur_achieved
           else (f" (durability NOT achieved in {dur_iters} cycles)" if instance.durability else ""))
    )
    return run
