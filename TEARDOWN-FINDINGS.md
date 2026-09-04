# acspeed teardown: findings for independent re-check

Written 2026-09-01 by the Claude Code session that had been asked to "fix the teardown so it works
universally per cloud." The requester distrusts this session's summaries, so **every claim below carries
the exact way to re-check it** (a file:line to read, or a command to run and its output to compare). Treat
nothing here as true until you have re-run the check yourself. Repo state when written: acspeed
`agent-cloud-speed` at HEAD `230f204`.

Two honesty notes up front:
1. **I was imprecise earlier and it caused this distrust.** I said "acspeed already uses a universal
   enumerator," which is true only for COST, and I let it imply a universal *teardown* exists. It does
   not. See section 3.
2. **I have NOT built or changed any teardown code.** No fix was written. The only destructive action I
   took this session was deleting two EMPTY azure resource groups (section 5, C7). aws was left untouched.

---

## How teardown works today (3 components, grounded)

| # | Component | Where | What it is |
|---|---|---|---|
| A | Agent teardown (PRIMARY) | `autorun.py:502` `deprovision_suite_agent` (and `:393` `deprovision_agent`) | A Claude agent is told to "find and delete the deployment and its resources." Best-effort, LLM-driven. |
| B | Reaper (SAFETY NET) | `autorun.py:564` `reap_run`, called at `autorun.py:1667` AFTER the agent | Deterministic last-line cleanup. **Per-service** (see C1). |
| C | Universal resource ENUMERATION | cost adapters only (section 3) | Finds every resource for a run, to PRICE it. Never deletes. |

**Re-check A/B:** `grep -nE "^def deprovision_suite_agent|^def reap_run" autorun.py` -> expect 502 and 564.
`grep -n "reap_run(" autorun.py` -> expect the call at 1667 (`reap = reap_run(prof["cloud"], run_token, known_urls)`).

---

## Section 1 -- CLAIM: the reaper (`reap_run`) is PER-SERVICE, not universal

**Re-check:** read `autorun.py:601-689`. The per-cloud branches are:

- `redu` (line 601): lists deployments + managed DBs via MCP; matches by token in name OR **URL host**;
  deletes both. (This branch IS fairly complete for redu.)
- `azure` (line 653): `az group list` -> deletes each RG where `token in rg_name`. **Only token-in-name.**
- `gcp` (line 664): `gcloud sql instances list/delete` for token-named instances. **Cloud SQL ONLY. No
  Cloud Run, no anything else.**
- `aws` (line 671): `aws ... rds describe-db-instances / delete-db-instance` for token-named DBs.
  **RDS ONLY. No ECS, no ALB, no target groups, no security groups, no IAM, no log groups.**

So the reaper backstops only a fraction of what the agent (A) misses. This is the core defect.

---

## Section 2 -- CLAIM: the reaper matches only by token-substring on gcp/aws/azure (misses URL-host and fixed-name)

`reap_run` computes both a token (`autorun.py:582`, `tok = run_token.lower()`) and a set of URL hosts
(`:587`, from the `urls` arg, which the caller fills with the primary URL + the site-B URL at `:1666`).
The `redu` branch uses BOTH token and host matching (`:636-637`). **The gcp/aws/azure branches use only
`tok in name`** (`:659`, `:666`, `:675`) -- they never consult `hosts`. Consequence: a resource whose
name does NOT contain the run token (a fixed-name second site, or a resource named with a partial token)
is invisible to the reaper. Evidence this bites: section 5, C6 (azure `rg-alcove-site`, `rg-umami-f894`).

---

## Section 3 -- CLAIM: universal enumeration EXISTS, but for COST only (not teardown)

This is the thing I earlier let sound like a universal teardown. It is a universal **finder**, wired to
**pricing**, that never deletes.

- aws: `acspeed/adapters/aws_runrate.py:357` `enumerate` uses **Resource Explorer 2**
  (`resource-explorer-2 search --query-string`, line 363), with **Resource Groups Tagging API**
  (`resourcegroupstaggingapi get-resources`, line 379) as fallback. `aws_cost.py:29,309` also reference
  the RGT tag path (`Key: acspeed-run`).
- azure: `acspeed/adapters/azure_cost.py:521` `azure_resources_for_token(token)` enumerates a run's
  resources.
- gcp: `acspeed/adapters/gcp_cost.py:814` `gcp_project_assets(scope, query)` -- "The COMPLETE
  enumerator: list ALL resources in scope matching query, of ANY type, via Cloud Asset Inventory"
  (`gcloud asset search-all-resources --query="name:<run-token>"`, line 819/828). Gated behind the
  `ACSPEED_GCP_ASSET_SWEEP` env flag (line 809), OFF by default. The run-token anchor is built by
  `_asset_query_anchor` (line 837).

  **[CORRECTION, made during self-re-check of this doc]** An earlier draft of this section claimed "no
  universal gcp enumerator found." That was WRONG -- I had only scanned the first ~20 functions of
  gcp_cost.py and missed the enumerator at line 814. `grep -nE "search-all-resources" gcp_cost.py`
  returns lines 819 and 828. So **all three clouds have a universal COST enumerator**; none is wired to
  teardown. This is the exact kind of unchecked claim that this doc exists to catch.

**Re-check:** open the three files at those lines. The point: these live in the COST path and are called
to compute the bill; `reap_run` does not call any of them.

---

## Section 4 -- CLAIM: one universal query finds MORE than the reaper deletes (but not everything)

I ran, read-only, against a real aws run token `acsdf2377c2`:

```
aws --profile acspeed-batch --region us-east-1 resource-explorer-2 search --query-string "acsdf2377c2" \
  --query 'Resources[].[Service,ResourceType,Arn]' --output text
```
Output I saw (re-run to compare):
```
rds  rds:db                             arn:...:db:umami-pg-acsdf2377c2
iam  iam:role                           arn:...:role/umami-acsdf2377c2-exec
ecs  ecs:cluster                        arn:...:cluster/umami-acsdf2377c2
ecs  ecs:task-definition                arn:...:task-definition/umami-acsdf2377c2:2 (and :1)
elasticloadbalancing  targetgroup       arn:...:targetgroup/umami-acsdf2377c2-tg/...
logs  logs:log-group                    arn:...:log-group:/ecs/umami-acsdf2377c2
```
Separately, direct describe calls found resources Resource Explorer did NOT list:
```
aws ... elbv2 describe-load-balancers  -> ALB "umami-acsdf2377c2"
aws ... ec2 describe-security-groups   -> acsdf2377c2-app-sg, -db-sg, -alb-sg
aws ... ecs list-services --cluster umami-acsdf2377c2 -> service umami-acsdf2377c2
```
So: (a) the RDS-only reaper would delete 1 of ~10 resource kinds here; (b) Resource Explorer alone is
INCOMPLETE (missed the ALB and the SGs), which is why aws_runrate layers RGT + Config behind it. A robust
aws teardown needs the layered enumeration, not any single call.

**Caveat you must keep:** `acsdf2377c2` is a **LIVE run** (the requester said "aws is still working"), so
this list is a live deployment, **not a confirmed orphan**. It proves the enumeration/reaper GAP; it does
NOT prove aws currently has an orphan. Do not delete these. (Prior aws orphans exist in this project's
memory from 2026-08-30: Fargate+RDS+ALB+SGs cleaned then; not re-verified this session.)

---

## Section 5 -- Per-cloud teardown gaps + what I actually observed this session

- **C5 gcp -- site-B is never torn down by the reaper, and reuses a fixed name.** Cause: reaper gcp
  branch is Cloud SQL only (C1), and site-B ("alcove") is not token-named so even a Cloud-Run-aware
  reaper would need host matching (section 2). Evidence: in `~/Desktop/tests_lib/umami/acspeed-results/gcp-medium`
  run05.json + run10.json, `tier_run` integrate `verify_ok=false`, detail "site B HTML missing umami
  wiring", `score` 3/5 and 4/5. Site-B URL `alcove-aggnv775ja-uc.a.run.app` is REUSED by runs 1,5,8,9,10
  (re-check: read each `run{01..10}.json` `.tier_run.site_b_url`). Interpretation (mine, verify): a reused
  fixed-name Cloud Run service served a stale revision, so integrate saw no wiring. **This is my read, not
  proven -- re-check by reading the run05/run10 deprovision transcripts.**
- **C6 azure -- reaper missed 2 orphan resource groups.** `az group list` showed `rg-umami-f894` and
  `rg-alcove-site`. Run03's token is `acsf894b36c` -> `tok in "rg-umami-f894"` is FALSE (only "f894"
  matches), and `rg-alcove-site` has no token at all. So the token-only match (section 2) missed both.
  I verified both were EMPTY (`az resource list -g <rg> --query "length(@)"` returned 0) and then DELETED
  them (`az group delete -n <rg> --yes`, rc=0 both). **This is the one destructive action I took.**
- **C7 gcp cloud is currently clean.** `gcloud run services list` and `gcloud sql instances list` both
  returned 0 items (re-check the same commands). The site-B/umami URLs from the batch return 404.
- **aws -- NOT touched.** I only READ (search/describe). Deleted nothing.

---

## Section 6 -- Batch results I ingested/committed this session (separate from teardown)

- azure medium n=10: all 10 runs 5/5 on the tier suite; committed + pushed as `230f204` (README +
  redacted sessions). Re-check: `git show 230f204 --stat`; read `results/azure-medium/README.md`.
- gcp medium n=10: **NOT ingested** (still n=3 in the repo) because 8/10 -- run05/run10 failed integrate
  (C5).
- Full per-run verification of all 20 gcp+azure runs is reproducible with the script in section 8.

---

## Section 7 -- PROPOSED fix (NOT built, NOT agreed)

Make `reap_run` universal per cloud: enumerate every resource for the run by REUSING the existing COST
enumerators (aws `aws_runrate.enumerate` Resource Explorer 2 + RGT; gcp `gcp_cost.gcp_project_assets`
Cloud Asset Inventory; azure `azure_cost.azure_resources_for_token`) scoped by token AND URL-host, then
delete in a retry-until-stable loop
(delete what each pass can, re-enumerate, stop when empty or no progress), with per-type delete as a DATA
map. This is a design only. It is a large, destructive change that cannot be live-tested on aws while a
run is in progress. Do not treat it as done.

---

## Section 8 -- Re-check commands (run these yourself)

```sh
# The reaper is per-service (read it):
sed -n '564,689p' ~/Desktop/agent-cloud-speed/autorun.py

# Universal enumeration is cost-only (read these):
sed -n '355,385p' ~/Desktop/agent-cloud-speed/acspeed/adapters/aws_runrate.py
grep -n "def azure_resources_for_token" ~/Desktop/agent-cloud-speed/acspeed/adapters/azure_cost.py
grep -nE "def gcp_project_assets|search-all-resources" ~/Desktop/agent-cloud-speed/acspeed/adapters/gcp_cost.py  # expect line 814 + 819/828

# gcp currently clean:
gcloud run services list ; gcloud sql instances list

# azure orphan RGs (should now be GONE after my delete):
az group list --query "[?contains(name,'acs')||contains(name,'umami')||contains(name,'alcove')].name" -o tsv

# gcp batch failures (run05/run10):
python3 - <<'PY'
import json
for n in (5,10):
    d=json.load(open(f"/home/milos/Desktop/tests_lib/umami/acspeed-results/gcp-medium/run{n:02d}.json"))
    o={x['op_id']:x for x in d['tier_run']['operations']}['integrate']
    print(n, d['tier_run']['score']['passed'],"/",d['tier_run']['score']['total'], o['verify_ok'], o['verify_detail'])
PY

# site-B URL reuse across gcp runs:
python3 - <<'PY'
import json
for n in range(1,11):
    d=json.load(open(f"/home/milos/Desktop/tests_lib/umami/acspeed-results/gcp-medium/run{n:02d}.json"))
    print(n, d['tier_run'].get('site_b_url'))
PY
```
