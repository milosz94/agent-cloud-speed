# The Reference-Optimal Gold, Authored and Published (Part 3 Section 5 open item)

Gold version: **provision-deploy/1.0.0**  |  Authored: 2026-08-28  |  Status: structure frozen, per-cloud
floor weights filled in Part 5 from measured runs.

This is the disclosed reference the paper's competitive ratio is measured against (Part 3
Section 2, "the gold reference is disclosed, and refutable where it is authored"; Section 5 open item, "the reference-optimal
schedule per task must be authored and published for the designed suite (the gold), with the refutation protocol
that updates it"). It is **cloud-independent**: the operation-state graph, the reference-optimal schedule,
and the floor-estimation rule are domain knowledge about how a deploy reaches its goal, not a property of
any one provider. Per-cloud floor weights are the only numbers filled per validation cloud, each from that
cloud's own measured runs, and every one is revisable downward (the refutation protocol below). No cloud is
named in this document. Part 5 fills the per-cloud floor weights, and the published wave names the three
validation clouds and reports the architecture strata each cell spans beside that cell's pooled mean.

The executable form of everything here is `acspeed/gold.py` (`part3_provision`) over `acspeed/reference.py`
(the pure Part-3 math). This document is the authored specification; the code is the instrument that computes
it from run records. The multi-operation gold the code also carries is specified for later waves and is not
used by the published wave (Section 6).

---

## 0. What "the gold" is, and is not

The gold is **not a single optimal makespan number**. Part 3 is explicit that the true optimum is not
computable in the open world, so what is published is an **interval between two measured references**, not
a proved bracket on it. The relation below holds only where the floor's own weights are no greater than what
an unobserved valid schedule attains (Part 3 Section 2), which a sample minimum over observed runs is not
shown to be:

```
    F_C   <=   true optimum   <=   best-achieved
   (floor)                        (DEA frontier)
```

and the reported efficiency is a **competitive-ratio INTERVAL**, never a point:

```
    competitive ratio of a run  as  [ M_actual / best_achieved ,  M_actual / F_C ]
                                     \___ lower arm ___/          \___ upper arm ___/
```

Reporting a single ratio against a self-declared bracket is internally inconsistent (a point estimate of an
interval-valued quantity); the interval is the reported object, and it takes the form of the operations-research
optimality-gap convention (a relaxation lower bound and any feasible upper bound sandwiching the optimum)
but not its guarantee: the floor here is a sample minimum, not a proved relaxation bound (Part 5 Section 3).
`acspeed` emits `competitive_ratio_interval = [best_ratio, floor_ratio]` per run for exactly this reason.

Precedent, stated plainly so novelty is not over-claimed:

- The **reference-based competitive score in a real competition** is the International Planning
  Competition's C*/C against a **participant-independent** reference plan (an optimal, or at-least-as-good,
  plan), with the best plan discovered used as the reference only as a documented fallback when no
  independent optimum is available (Taitler et al., the 2023 IPC, AI Magazine 2024). Our floor is NOT the
  participant-independent case. It stands on the fallback's side of that distinction without sharing its
  mechanism, that fallback being a documented last resort where ours is a standing rule that revises toward
  the best observed whenever a trace undercuts it. It is measured rather than authored, but it is one run's
  own critical-platform time. We do not claim IPC systematically revises its reference; we borrow its C*/C
  competitive scoring and apply it to wall-clock makespan.
- The **closest prior efficiency-as-ratio-to-a-reference-trace** metric is OSWorld-Human's Weighted
  Efficiency Score, a ratio `t_human / t_agent` of a **human-reference** trajectory's step count to the
  agent's (Abhyankar, Qi and Zhang, arXiv 2506.16042, 2025). We differ on the axes that matter here: their
  reference is a **human-annotated** trajectory in **step count**; ours is a **wall-clock critical-path
  makespan** with a platform/agent split, and we report the reference as an **interval** rather than assert a single
  reference value.
- The floor's "reduces to the critical path under unlimited concurrency" is the **span law** (Graham 1969;
  Brent 1974; Blumofe and Leiserson 1999), and the best-achieved frontier is the **DEA best-practice
  frontier** (Charnes, Cooper and Rhodes 1978). Neither is ours; the composition on cloud operations in
  wall-clock is.

---

## 1. The operation-state graph the published wave uses

A "deploy the app" session is a trajectory through an operation-state graph (Part 3 Section 1): nodes are
task world-states, edges are Part-2 operations, edge weight is the operation's critical-path wall-clock. The
published wave scores every cell against the **degenerate one-operation provision graph** (`part3_provision`):
one provision edge from the start state to the served state, whatever architecture the agent provisioned, so
selection excess is 0 by construction on every published run (Part 5, Section 3).

```
     [ start ] -------------------- provision (one edge) --------------------> [ served ]
                         weight: the operation's critical-path wall-clock
```

- **Goal predicate** (the "served" state): the app answers at its URL under Part 2's app-serving end line, AND
  the off-clock success oracle finds no known infrastructure-error or default-page signature, and no
  effectively empty body, in the served root (Part 5, Section 1). The liveness predicate that times it was
  amended twice mid-wave and now stops the clock at the first durable serve; Part 5 Sections 1 and 3 state
  each form and how many of the 94 published runs it timed.
- **The reference-optimal schedule** on this graph is the single provision edge at its floor weight, so
  `F_C` is that edge's floor weight (Section 2), and everything a run spends above it is execution excess
  (Part 3 Section 3). The floor is one per cell rather than one per architecture stratum, so where the run
  that sets it provisioned a different bundle from the rest of the cell, the gap between the two bundles
  lands in the execution excess, making it a distance to a cheaper bundle as well as to the same bundle's own
  floor. That happens on two of the nine published cells (Part 5, Section 3).

---

## 2. Floor-weight estimation (per cloud, revisable)

Each floor weight is the **minimum-observed critical-platform time** for that operation across clean
goal-reaching runs (Part 3 Section 2). Properties, all disclosed with the number:

- Its bias has **no established direction**: taking a minimum over runs pushes the estimate above the
  analytic floor, while charging concurrent platform work to the agent under the chain rule pushes it
  below. It is always reported with its `n`
  (`floor_estimated_from_n`), never as a bare point, and it carries no interval of its own (Part 3,
  Section 2).
- **Excluded from the floor and the frontier**: runs that never served (they carry a low makespan from a
  failed attempt), and, under gold 1.0.0, every run flagged `served_on_first_poll` (a possible leftover
  deployment or a false-early t1). Gold 1.1.0 narrowed that second exclusion, and it is the narrowed rule
  `acspeed/gold.py::_is_suspect` implements today: a first-poll run is excluded only when the status that
  stopped its clock was a 4xx or 5xx, when that status cannot be read, or when its served URL does not
  carry the run's own token. A first-poll 2xx or 3xx whose URL carries its own token is admitted, because
  excluding it drops an entire architecture class from the floor and the frontier at any `n`. Both
  exclusions guard against spuriously dragging the floor down.
- A run whose measured provision leg falls **below** its authored floor **refutes** that floor
  (Part 3 Section 2): the floor is to be revised down to the observed leg and every ratio re-attributed.
  `part3_provision`, the gold that carries every published number, builds each run's `Edge` and calls
  `two_ratios` before it reaches `gold_is_refuted`, and both raise on a below-floor value first
  (`reference.py:64`, `reference.py:204`), so its `refutation.refuted` can only ever be emitted `False`,
  as it is in all nine published cells. The floor still descends there, 14 times in the published wave,
  but by `_provision_weights` recomputing the sample minimum over a larger pool rather than by the
  refutation check firing.

### Versioning (a revisable floor requires it)

Because the floor moves, an **unlabelled ratio is not comparable across revisions**. Every reported ratio is
therefore bound to `(gold_version, owner rule, threshold, n)`:

- `gold_version` changes only when the **graph structure or the floor-estimation
  rule** changes. The per-run floor moving downward under the refutation protocol is a within-version event,
  disclosed by `floor_estimated_from_n` and the `refutation` block, not a version bump.
- Part 5 does not pre-register these: "**This wave is not pre-registered**" (Part 5, Section 3), and a dated
  amendment log is published in place of a registration. Of the four objects a registration would have
  covered, **two were fixed in the harness's committed code before the first published run and are unchanged
  across all 94 runs** (the graph structure and the off-clock success oracle); **the other two were amended
  mid-study** (the floor-estimation rule, whose narrowed first-poll exclusion bumped the gold version
  1.0.0 -> 1.1.0, and the liveness predicate, amended twice). Those landed 2026-09-04, after 79 of the 94
  published runs were measured. Outside those four objects, the owner split was amended the same day
  (inside an operation window a gap ending in an agent event goes to the agent up to a 60 s threshold and to
  the platform past it), and the Medium tier's integrate verifier on 2026-09-05. The ratio is reported bound
  to `(gold_version, owner rule, threshold, n)`, the owner rule and threshold being that split and its 60 s.

### Floor sensitivity

Because the competitive ratio is only as trustworthy as its estimated denominator, `acspeed` reports
`floor_sensitivity`: the bracket ratio recomputed at `F_C * (1 +/- 0.10)`. It cannot tell a
denominator-driven cell from a stable one: the bracket ratio is `best_achieved / F_C`, so perturbing the
denominator rescales every cell by the same `1/0.9` and `1/1.1` (Part 5, Section 3). What it establishes
is the leverage, that a 10 percent error in `F_C` is an error of at most 11.1 percent in the ratio, which
bounds the denominator's arithmetic influence and is not an answer to the objection that an estimated
optimum is tuned. It is computed alongside every gold instance and carried into no released run record.

---

## 3. Per tier

### Easy (provision + serve)

- Graph: the one-operation provision graph of Section 1: F_C = min-observed critical-platform,
  interval = [F_C, best-achieved], selection excess 0.
- Reported: the realized makespan and its ratio to the best achieved are counterfactual-free; per-operation
  execution excess needs no graph-wide optimum but is taken against the operation's floor weight; F_C, the
  floor ratio and selection excess are the admitted counterfactual (bounded, revisable). The ratio interval
  is a within-cell statistic beside the per-task total M, not the headline (Part 5, Table 5.1).

### Medium (provision x2 + operate-mutate + cross-service verify + restart-durability)

- The agent stands up the app, registers a user, stands up a **second** app, wires the two, a headless
  visit must register end to end, and then it **restarts the services and the earlier state must survive**.
  The published wave scores its six Medium cells by `part3_provision` on the deploy operation alone, so their
  selection excess is 0 like every other cell (Part 5, Section 3); the multi-operation gold this tier would
  call for is specified for later waves (Section 6).
- The restart-durability step is an OPERATE-MUTATE edge whose goal predicate is "serves again AND state
  survived": a deploy that put state on ephemeral storage (a co-located datastore container on a stateless
  serverless surface) meets the earlier serving goal but FAILS this wider goal, the set-relativity of Part 3
  Section 5 made concrete and cheap (no chaos tooling).

---

## 4. What the gold does NOT include, and why (no human baseline)

Rigorous benchmark reviews expect a human-expert baseline on the frontier as an independent anchor
(Wei et al., ICML 2025, on the rigor a human baseline needs; OSWorld-Human's human trajectories). This
suite does not carry one:

1. The **anchor is the frozen reference agent**, run under the same harness across all clouds. A matched
   block (Part 4 Section 2) holds the task and the goal identical and varies only the cloud, so the
   comparison is attributable to the cloud as this agent met it in the ten days measured and without a human
   in the loop.
2. A human operator adds **uncontrolled variance** (skill, familiarity, fatigue, hand-timing) to a
   measurement whose whole value is reproducibility. That is this suite's reason for running none, and it is
   not Wei et al.'s position: they ask for rigor and transparency in human baselines rather than for their
   omission, and a transparently reported baseline of less than maximal rigor would still have supplied an
   independent anchor, so the absence is a cost rather than a saving (Part 5, Section 2).
3. The reference-optimal **floor** plays part of the role a human anchor usually plays (a target the agent
   is measured against), and it is data-derived and revisable rather than a single expert's run.

The frozen reference agent is disclosed (model id and prompt; its version is not pinned, Part 5, Section 2).

Also out of scope, each disclosed: account-plane and ability-to-pay steps (Part 2; no run of the published
wave took an account-plane step; where a cloud refused a request for lack of quota or at an account limit,
the agent worked around it, the time that took is inside Part 4's per-task total M, and Part 5, Section 6
counts those refusals); cross-task parallelism and shared setup (each task is an isolated block, Part 4
Section 3); and, on a serverless surface with no reachable shell, any in-guest measurement. The reported axes
are the four of Part 5, Section 4, each N/A disclosed and never faked.

---

## 5. Refutation protocol (how the gold is revised)

1. Publish, per task and per cloud, `(gold_version, F_C, floor_estimated_from_n, bracket, floor_sensitivity)`.
2. On every new run pool, re-estimate F_C as the sample minimum of that pool's critical-platform times: a
   clean goal-reaching trace with a lower critical-platform time **lowers the floor**, the
   selection/execution split is re-attributed, and the result republished under the same `gold_version`
   with a larger `n`. This is how the floor moved 14 times in the published wave. `gold_is_refuted` is the
   separate check for the stronger event, a trace whose whole makespan undercuts the published F_C. On the
   one-operation gold it cannot report that event: `part3_provision` reaches it only after `Edge` and
   `two_ratios` have already raised on any below-floor value, and a makespan is never below its own run's
   critical-platform time, so it returns `False` on every pool it can be reached on.
3. If the **graph structure** or the **floor-estimation rule** changes (not just the numeric floor), bump
   `gold_version` and re-report every ratio under the new version. Never compare ratios across versions.
4. Off the designed suite, report only per-operation execution excess (each operation against its own
   floor); selection excess needs the enumerable goal-reaching graph and is not reported there.

---

## 6. Map to the code

| Gold element | acspeed |
|---|---|
| Pure Part-3 math (graph, floor, two ratios, refutation) | `acspeed/reference.py` |
| The one-operation provision gold from run history (every published number) | `gold.part3_provision` |
| Competitive-ratio INTERVAL, versioned gold, floor sensitivity, refutation | fields on the gold output |
| Rendered into the review tables | `build_tables.py` (Part 3 line) |
| Specified for later waves, not used by the published wave: the multi-operation provision-deploy graph with concurrent, serial and colocated alternatives, and its per-decision selection/execution scoring | `gold.build_deploy_graph`, `gold.ProvisionAlternative`, `gold.score_deploy_run`, `gold.part3_deploy` |

Every number in this gold is produced by that code from real run records; none is authored by hand beyond
the graph structure, which is the domain knowledge Part 3 Section 2 layer 1 scopes to the designed suite.
