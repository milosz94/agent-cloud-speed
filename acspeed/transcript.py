"""Part 5 (validation harness): reconstruct a timed, owner-labelled trace from an
agent session transcript, so the Part 1-4 core can be run on a real run.

An agent drives a cloud through a tool interface (here, an MCP session). The
session is recorded as a JSON-lines transcript: one row per streamed content
block, each row carrying a ``timestamp`` and, for a turn, a shared request id.
This module turns that transcript into a list of :class:`~acspeed.types.Span`
that ``criticalpath`` and ``agenttime`` consume unchanged.

THE ATTRIBUTION RULE (the whole method in one sentence). A turn keeps streaming
while its first tool call already runs, so model generation and tool execution
OVERLAP in wall-clock; summing raw spans would double-count. Instead every gap
between two consecutive events is charged to the lane of the event that ENDS it,
so each second is attributed exactly once and the owner durations sum to wall by
construction. Consequences carried honestly:

* AGENT time is a FLOOR and PLATFORM time a CEILING: a gap ending in a tool
  result is charged wholly to the platform even if the model was still streaming
  through part of it.
* Percentages are of WALL, never of attributed-only time. Dividing by attributed
  would drop held-out idle from the denominator, so a slower platform would
  mechanically raise the agent's share. Idle gaps therefore stay in the chain as
  an ``"idle"``-owned span, which keeps ``makespan == wall``.
* A single serial session has no genuine overlap, so ``overlap`` falls out as ~0
  rather than being fabricated; the same core measures real overlap the day a run
  has true concurrency (parallel tool calls), with no change here.

THE LABELLING TRAPS (each one a lane error the sum-to-wall identity can NEVER
catch, which is why the tests below assert the labeller with positive controls,
not only the identity):

* A backgrounded readiness poll finishes and returns as a bare user row; naively
  that reads as a person. It is the PLATFORM reporting back
  (``origin.kind == "task-notification"`` or ``promptSource == "system"``).
* ``AskUserQuestion`` is a tool whose result arrives when a person answers, so
  its result row looks like the platform. It is HUMAN.
* A retry is charged to whoever performed it (the agent), UNLESS the platform
  returned a definitively broken response (a hard error status or a malformed
  result, a decidable codebook), when the wasted platform work is platform-owned.
  This broken-response criterion is the ONE knob that moves blame between owners;
  the paper defends it empirically (Part 1, "Defending the owner split
  empirically") with inter-annotator agreement on the codebook (two annotators,
  Cohen's kappa) and a sensitivity analysis of the split as the threshold moves.
  acspeed applies the criterion in the trace it is given; the annotator study and
  the threshold sweep are the Part-5 method, not yet wired here.

The method is cloud-agnostic: owner attribution depends on transcript structure,
not on any cloud's tool names. The one tool-name rule (``AskUserQuestion``) names
a generic agent tool, not a cloud primitive. Token accounting is deliberately
separate (:func:`dedupe_token_usage`): tokens are exact from the transcript, any
currency price is an assumed rate reported elsewhere (Part 4's frontier input).
"""
from __future__ import annotations

import glob as _glob
import json
import os
from datetime import datetime
from typing import Dict, List, Optional, Sequence

from .criticalpath import owner_split
from .types import AGENT, PLATFORM, Span

_EPS = 1e-9

HUMAN = "human"
IDLE = "idle"
DEFAULT_IDLE_CAP = 300.0  # seconds; a longer HUMAN gap is held-out idle, charged to no lane
                          # (a platform-ending gap is a blocking cloud call's latency: platform, no cap)
MAX_GEN_GAP = 60.0        # seconds; a gap that ENDS in an agent turn but is longer than this is NOT model
                          # generation and is the agent sitting IDLE while the operation's platform work
                          # proceeds (measured: clean-run agent gaps top out ~36s; idle-waits are 64-191s).
                          # Idle-while-the-cloud-works is platform critical-path time, not agent time: the
                          # split must not reward an agent that blocks in one long poll command (already
                          # platform) over one that pauses and re-polls in the next turn (was agent) for the
                          # SAME real cloud wait. Measured cause: azure-medium-a run10 idled 191s ("I'll wait
                          # for the completion notification") -> charged to agent -> platform read 124s and
                          # became a false floor. This is the operational form of the Part 1 overlap limit.
ASK_TOOL = "AskUserQuestion"
_KEEP_TYPES = ("assistant", "user")
_USAGE_KEYS = ("input_tokens", "output_tokens",
               "cache_read_input_tokens", "cache_creation_input_tokens")


# --------------------------------------------------------------------------------------------------
# Transcript parsing (schema-minimal: only the fields the attribution needs)
# --------------------------------------------------------------------------------------------------

# Codex writes its session rollout as {"timestamp", "type", "payload"} rather than Claude Code's
# assistant/user rows. The ATTRIBUTION RULE above is about lanes and gaps, not about either vendor's
# JSON, so the agent-agnostic move is to normalize at load time: every row below becomes the canonical
# shape, and lane_of / _spans_from_rows / agenttime then run byte-identically for both agents. That
# keeps ONE measurement implementation rather than a second one that has to be kept in step.
_CODEX_ROW_TYPES = {"response_item", "event_msg", "session_meta", "turn_context"}
# A gap ENDING on one of these ends with the model having produced something: agent lane.
_CODEX_AGENT = {"message", "agent_message", "reasoning"}
# A gap ENDING on one of these ends with a tool/environment result: platform lane, exactly as a
# Claude Code tool_result does.
_CODEX_PLATFORM = {"function_call_output", "custom_tool_call_output", "patch_apply_end",
                   "exec_command_end", "mcp_tool_call_end", "web_search_end"}


def _is_codex_rollout(rows: Sequence[dict]) -> bool:
    """True when these raw rows are a Codex rollout rather than a Claude Code transcript."""
    seen = {r.get("type") for r in rows[:50]}
    return bool(seen & _CODEX_ROW_TYPES) and any("payload" in r for r in rows[:50])


def _normalize_codex(raw: Sequence[dict]) -> List[dict]:
    """Map Codex rollout rows onto the canonical assistant/user rows the rest of this module reads."""
    out: List[dict] = []
    for d in raw:
        ts = d.get("timestamp")
        pay = d.get("payload")
        if not ts or not isinstance(pay, dict):
            continue
        kind = pay.get("type")
        if kind in _CODEX_AGENT:
            out.append({"type": "assistant", "timestamp": ts, "message": {"content": []}})
        elif kind in _CODEX_PLATFORM:
            # An unknown tool id is fine: lane_of only special-cases the ask-the-human tool, and
            # names.get(None) is None, so anything else lands on the platform lane as intended.
            out.append({"type": "user", "timestamp": ts,
                        "message": {"content": [{"type": "tool_result",
                                                 "tool_use_id": pay.get("call_id") or pay.get("id")}]}})
        elif kind == "user_message":
            out.append({"type": "user", "timestamp": ts, "message": {"content": []}})
    return out


def _load_rows(path: str) -> List[dict]:
    """Rows of type assistant/user carrying a timestamp, sorted by time. Everything else in the
    transcript (summaries, snapshots, mode markers) is not a timed event and is dropped.

    Accepts either a Claude Code transcript or a Codex rollout; a Codex rollout is normalized to the
    same row shape first, so everything downstream is agent-agnostic."""
    raw: List[dict] = []
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            raw.append(d)
    if _is_codex_rollout(raw):
        rows = _normalize_codex(raw)
    else:
        rows = [d for d in raw if d.get("type") in _KEEP_TYPES and d.get("timestamp")]
    rows.sort(key=lambda r: r["timestamp"])
    return rows


def _epoch(row: dict) -> float:
    return datetime.fromisoformat(str(row["timestamp"]).replace("Z", "+00:00")).timestamp()


def _blocks(row: dict) -> List[dict]:
    content = (row.get("message") or {}).get("content")
    return [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []


def _tool_names(rows: Sequence[dict]) -> Dict[str, str]:
    """Map each tool_use block id to its tool name (needed only to spot AskUserQuestion results)."""
    names: Dict[str, str] = {}
    for r in rows:
        for b in _blocks(r):
            if b.get("type") == "tool_use":
                names[b.get("id")] = b.get("name")
    return names


def lane_of(row: dict, tool_names: Dict[str, str]) -> str:
    """The owner of the gap that ENDS on ``row``: ``agent`` / ``platform`` / ``human``.

    Read the two labelling traps in the module docstring before changing this.
    """
    if row.get("type") == "assistant":
        return AGENT
    results = [b for b in _blocks(row) if b.get("type") == "tool_result"]
    if results:
        name = tool_names.get(results[0].get("tool_use_id"))
        # A person answering a question is HUMAN, even though it arrives as a tool result.
        return HUMAN if name == ASK_TOOL else PLATFORM
    # A bare user row: a background command finishing is the PLATFORM reporting back, not a person.
    origin = row.get("origin") if isinstance(row.get("origin"), dict) else {}
    if origin.get("kind") == "task-notification" or row.get("promptSource") == "system":
        return PLATFORM
    return HUMAN


# --------------------------------------------------------------------------------------------------
# Transcript -> owner-labelled trace
# --------------------------------------------------------------------------------------------------

def clip_rows(rows: Sequence[dict], until_epoch: Optional[float],
              since_epoch: Optional[float] = None) -> List[dict]:
    """Rows in the window ``(since_epoch, until_epoch]`` (Unix times). Part 2 ends an operation at its
    slot-5 end signal (app-serving readiness, an EXTERNAL poll); events the agent produces after that
    instant (post-serving verification, note-taking) are outside the operation and must not be
    attributed to it. ``since_epoch`` opens the window at a later operation's start signal, so a resumed
    session can be sliced per operation. ``None`` on either bound leaves that side open."""
    out = list(rows)
    if since_epoch is not None:
        out = [r for r in out if _epoch(r) >= since_epoch - _EPS]
    if until_epoch is not None:
        out = [r for r in out if _epoch(r) <= until_epoch + _EPS]
    return out


def trace_from_transcript(path: str, cap: float = DEFAULT_IDLE_CAP,
                          until_epoch: Optional[float] = None,
                          since_epoch: Optional[float] = None,
                          idle_is_platform: bool = False) -> List[Span]:
    """Reconstruct the run as a linear chain of gap-spans, each owned by the lane of the event that
    ends it. A gap longer than ``cap`` is held-out idle (owner ``"idle"``) UNLESS it ends in a platform
    event (a blocking cloud call's observed latency, which is platform-time at any length); either way
    it stays in the chain, so the makespan equals wall-clock. ``until_epoch`` / ``since_epoch`` clip to a
    single operation's window (its start/end signals, see ``clip_rows``). Returns ``[]`` for a window
    with fewer than two timed events.
    """
    rows = clip_rows(_load_rows(path), until_epoch, since_epoch)
    return _spans_from_rows(rows, cap, idle_is_platform)


def _spans_from_rows(rows: Sequence[dict], cap: float, idle_is_platform: bool = False) -> List[Span]:
    if len(rows) < 2:
        return []
    tool_names = _tool_names(rows)
    spans: List[Span] = []
    prev_id: Optional[str] = None
    for i, (prev, cur) in enumerate(zip(rows, rows[1:])):
        gap = _epoch(cur) - _epoch(prev)
        if gap < 0:
            gap = 0.0
        lane = lane_of(cur, tool_names)
        # A platform-ending gap is the client-observed latency of a blocking cloud call (a
        # provisioning/readiness poll that blocks for minutes): platform critical-path time at ANY length
        # (platform is a ceiling; capping it dropped real provisioning wall by ~2.5x). An agent-ending gap
        # up to MAX_GEN_GAP is model generation (agent). A LONGER agent-ending gap is the agent sitting idle
        # -- but that only means "the cloud is doing the work" INSIDE a single provisioning operation
        # window (`idle_is_platform`, set by the per-operation split); across a raw multi-op trace the same
        # gap is an inter-operation pause and stays held-out idle. A human (ask-tool) gap over the idle cap
        # is a genuine person-pause, always held out.
        if lane == PLATFORM:
            owner, kind = PLATFORM, ""
        elif lane == AGENT:
            if idle_is_platform and gap > MAX_GEN_GAP:
                owner, kind = PLATFORM, ""          # idle-while-the-cloud-works, inside an operation window
            elif gap > cap:
                owner, kind = IDLE, ""               # a very long agent gap outside an op window: held out
            else:
                owner, kind = AGENT, "inference"
        else:  # HUMAN
            owner, kind = (IDLE, "") if gap > cap else (HUMAN, "")
        sid = "g%d" % i
        spans.append(Span(id=sid, duration=gap, owner=owner,
                          deps=() if prev_id is None else (prev_id,), kind=kind))
        prev_id = sid
    return spans


def _agent_first_token_split(rows: Sequence[dict], cap: float,
                             idle_is_platform: bool = False) -> Dict[str, float]:
    """Within the agent lane, time before the FIRST block of a turn is round-trip latency
    (first-token); the rest is visible streaming. Never called 'thinking'."""
    tool_names = _tool_names(rows)
    first_uuid: Dict[str, str] = {}
    for r in rows:
        rid = r.get("requestId")
        if rid and rid not in first_uuid:
            first_uuid[rid] = r.get("uuid")
    ft = st = 0.0
    # Inside an operation window an agent gap over MAX_GEN_GAP is idle-while-the-cloud-works (platform in
    # the main split), not generation, so it must be excluded here too or the agent sub-breakdown would
    # not reconcile with the owner split.
    limit = MAX_GEN_GAP if idle_is_platform else cap
    for prev, cur in zip(rows, rows[1:]):
        gap = _epoch(cur) - _epoch(prev)
        if gap < 0 or gap > limit:
            continue
        if lane_of(cur, tool_names) == AGENT:
            rid = cur.get("requestId")
            if first_uuid.get(rid) == cur.get("uuid"):
                ft += gap
            else:
                st += gap
    return {"first-token": round(ft, 1), "streaming": round(st, 1)}


def lane_summary(path: str, cap: float = DEFAULT_IDLE_CAP,
                 idle_is_platform: bool = False) -> Optional[dict]:
    """The agent-vs-platform split of one run's wall-clock, over WALL (not attributed).

    Returns lane seconds and percent-of-wall for agent/platform/human, held-out idle, the agent
    first-token/streaming split, and the floor/ceiling caveat. ``None`` if the transcript has fewer
    than two timed events. The numbers reproduce the reference attribution engine by construction.
    """
    rows = _load_rows(path)
    if len(rows) < 2:
        return None
    spans = _spans_from_rows(rows, cap, idle_is_platform)
    split = owner_split(spans)
    makespan = split["makespan"]  # == wall for a chain that includes the idle spans
    owners = split["owners"]
    lanes = {lane: round(owners.get(lane, {}).get("critical", 0.0), 1)
             for lane in (AGENT, PLATFORM, HUMAN)}
    idle_s = round(owners.get(IDLE, {}).get("critical", 0.0), 1)
    pct = {lane: (round(100.0 * v / makespan, 1) if makespan > _EPS else 0.0)
           for lane, v in lanes.items()}
    return {
        "path": os.path.basename(path),
        "wall_s": round(makespan, 1),
        "idle_s": idle_s,
        "attributed_s": round(sum(lanes.values()), 1),
        "lanes": lanes,
        "lane_pct_of_wall": pct,
        "agent_split": _agent_first_token_split(rows, cap, idle_is_platform),
        "events": len(rows),
        # agent is a floor and platform a ceiling: see the module docstring.
        "agent_is_floor": True,
        "platform_is_ceiling": True,
    }


def idle_cap_sensitivity(path: str, caps: Sequence[float] = (600, 300, 200, 150, 120, 60)) -> dict:
    """Agent percent-of-wall as the idle cap varies. A result that moves a lot with the cap is
    idle-cap-fragile and must be reported with the cap, never as a bare number."""
    out = {}
    for c in caps:
        s = lane_summary(path, cap=float(c))
        out[float(c)] = None if s is None else s["lane_pct_of_wall"][AGENT]
    return out


# --------------------------------------------------------------------------------------------------
# Token accounting (separate from timing: exact tokens now, priced currency in Part 4)
# --------------------------------------------------------------------------------------------------

def dedupe_token_usage(path: str) -> dict:
    """Sum token usage with the per-streamed-block inflation removed.

    The transcript repeats the SAME usage object on every content-block row of a turn, so summing
    rows counts a multi-block turn several times over (measured inflation is large AND varies per
    run, so it does not cancel out of a ratio). Dedupe by request id (falling back to message id,
    then row uuid) and take the max per component (guarding a partial early row), then sum the turns.
    """
    rows = _load_rows(path)
    per_turn: Dict[str, Dict[str, int]] = {}
    for r in rows:
        if r.get("type") != "assistant":
            continue
        msg = r.get("message") or {}
        usage = msg.get("usage")
        if not isinstance(usage, dict):
            continue
        key = r.get("requestId") or msg.get("id") or r.get("uuid")
        cur = per_turn.setdefault(key, {})
        for k in _USAGE_KEYS:
            v = usage.get(k) or 0
            if v > cur.get(k, 0):
                cur[k] = v
    totals = {k: 0 for k in _USAGE_KEYS}
    for cur in per_turn.values():
        for k in _USAGE_KEYS:
            totals[k] += cur.get(k, 0)
    totals["turns"] = len(per_turn)
    return totals


# --------------------------------------------------------------------------------------------------
# Transcript selection (an instrument that picks among candidates must say HOW it picked)
# --------------------------------------------------------------------------------------------------

def pick_transcript_by_time(directory: str, near_epoch: float, pattern: str = "*.jsonl") -> dict:
    """Choose the transcript whose [first, last] timestamp span COVERS ``near_epoch`` (the run's
    known start), never the largest file. A slug deployed twice has two transcripts; picking by size
    once measured the wrong day. When more than one covers the anchor the pick is a guess and is
    labelled as such, never chosen silently."""
    candidates = sorted(_glob.glob(os.path.join(directory, pattern)))
    covering = []
    for p in candidates:
        rows = _load_rows(p)
        if len(rows) < 2:
            continue
        first, last = _epoch(rows[0]), _epoch(rows[-1])
        if first - _EPS <= near_epoch <= last + _EPS:
            covering.append((p, first, last))
    if len(covering) == 1:
        return {"path": covering[0][0], "chosen_by": "time-cover", "ambiguous": False}
    if len(covering) > 1:
        best = min(covering, key=lambda t: abs((t[1] + t[2]) / 2.0 - near_epoch))
        return {"path": best[0], "chosen_by": "time-cover-nearest-midpoint(guess)",
                "ambiguous": True, "candidates": [c[0] for c in covering]}
    return {"path": None, "chosen_by": "none-cover", "ambiguous": True, "candidates": candidates}
