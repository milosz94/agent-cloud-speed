# Playbook: process an acspeed run into `results/`

How to turn a raw `acspeed-run` output directory into a published entry in this folder. Everything here is
off-clock (it never affects a measured number). Do it after each run or batch.

## 0. What a run produces

`acspeed-run --adapter <cloud> --model claude-opus-5` writes to `<app-dir>/acspeed-results/<cloud>/`:

```
run01.json            one per run: outcome, split (platform/agent/boot), steps, tokens, cost_run_rate, url
run01_deploy.bootlog  the microVM serial console for the deploy turn
run01_sessions/projects/-home-agent-app/*.jsonl   the agent transcripts (deploy [+ repair/deprovision])
tables.txt / tables.json                           the rendered tables (build_tables.py output)
```

Runs accumulate: a second invocation adds `run02.json`, etc. Failed runs stay in the dir; the fairness
filter below decides what gets published.

## 1. The fairness filter (what is publishable)

A run is published ONLY if all of these hold. A run that fails any is real data but would misrepresent the
cloud, so it is left out (and the exclusion is stated).

- **served**: `outcome == SUCCESS` and a `url` that answered HTTP < 500 (external poll). `FAILURE-*` is out.
- **content-verified**: the served root is real app content, not an infra/default page (off-clock oracle).
- **priced**: `cost_run_rate` is present and its `unpriced_resources` is empty (or the unpriced items are
  disclosed). A run the cost tool could not price is not comparable.
- **tokens present**: the agent token count survived (needed for the agent-$ column).
- **not host-contended**: the run was not slowed by test-host disk/CPU contention (tell: a boot-time spike
  well above the cloud's norm on a shared disk). Contention is our problem, not the cloud's.

Quick triage of a dir:

```bash
python3 - <<'PY'
import json, glob, os
for f in sorted(glob.glob("acspeed-results/<cloud>/run*.json")):
    d = json.load(open(f)); crr = d.get("cost_run_rate") or {}
    print(os.path.basename(f), d.get("outcome"), "url=", bool(d.get("url")),
          "priced=", crr.get("kind") is not None, "unpriced=", crr.get("unpriced_resources") or [])
PY
```

## 2. Inspect the numbers

```bash
python3 build_tables.py --dir /path/to/acspeed-results/<cloud>
```

This renders the headline + per-run table (all numbers via acspeed). Use it to read t1, the platform/agent
split, steps, tokens, agent-$, and the Part-4 cost. Foreign/old-schema runs are auto-skipped.

## 3. Redact the fair runs' transcripts

`acspeed sessions` strips credentials AND infrastructure (control-plane hosts, internal IPs, tech names)
and REFUSES to write if a secret survives. Run it per fair run, pointing `--in` at the transcript dir:

```bash
python3 -m acspeed.cli sessions \
  --in  /path/to/acspeed-results/<cloud>/run01_sessions/projects/-home-agent-app \
  --out results/<cloud>/sessions
```

It prints `inputs / written / total_redactions / residue`. **`residue` MUST be 0** - if not, do not
publish; the redactor found something it could not scrub. (Add `--keep-substrate` only for an internal
bundle; never for a public one.) For n>1, redact each fair run's sessions into the same `results/<cloud>/
sessions` (it writes a `REDACTION-MANIFEST.json`).

## 4. Write the cloud's ONE table

Each cloud has ONE table in `results/<cloud>/README.md`, one row per fair run, merging performance and cost:
`run | t1 (s) | platform (s) | agent (s) | steps | tokens | agent $ | fixed $/mo | $/mo @ 10k req | $/mo @
500k req | $/mo @ 10M req`. `fixed $/mo` is the flat / always-on floor from `cost_run_rate`; the `$/mo @ N
req` columns are the traffic estimates (total monthly cost at that volume). A fixed VM is flat, so its three
`$/mo` columns all equal its `fixed $/mo`; a serverless front rises with traffic. Every money value carries
a `$` sign. One table + a one-line note (architecture + how the cost behaves with traffic), nothing else.

## 5. Verify before committing

```bash
# no em-dashes anywhere (the repo test bans them)
grep -rlP $'\x{2014}|\x{2013}|\x{2015}' results/ && echo "DASH FOUND - fix" || echo "dash-clean"
# no secret residue in any redacted bundle
grep -rl '"residue": [^0]' results/*/sessions/REDACTION-MANIFEST.json && echo "RESIDUE - do not publish"
cd .. && python3 -m unittest discover -s tests -q   # suite still green
```

## 6. Commit + push

```bash
git add results/
git commit -m "docs(results): <cloud> run(s) YYYY-MM-DD (n=N, fair)"
git push origin main   # private repo; push freely
```

## Notes

- **Never fabricate or reconstruct a cost.** Every figure must come from `cost_run_rate` (a live list
  price). If a run is unpriced, exclude it or disclose the unpriced item by type - never a silent $0.
- **redu is an adapter + validation baseline, never a paper subject.** It can live in `results/` (private
  repo); the paper anonymizes clouds and never names redu.
- **The dataset grows.** Re-run this playbook after each batch; the per-cloud table gains rows as more fair
  runs land. n=1 is a single observation, not a distribution - say so until n is real.
