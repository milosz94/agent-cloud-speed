# Results: umami deploys, easy + medium tier (2026-08-30)

An agent deploys the umami app + its database to a public URL on each cloud; `acspeed` measures each run
externally. App: umami. Model: `claude-opus-5`. n=10 for redu, aws, gcp and azure (all runs through the
corrected cost tool). How runs land here: `PLAYBOOK.md`.

Each cloud has its own one-table result:

- [redu](redu/) - fixed VM + managed Postgres (n=10)
- [aws](aws/) - Fargate/EC2 + ALB + RDS + public IPv4 (n=10)
- [gcp](gcp/) - Cloud Run (min-instances=1) + Cloud SQL + public IPv4 (n=10)
- [azure](azure/) - App Service or Container Apps (agent's choice) + managed Postgres (n=10)

**Medium tier** goes past "did it deploy": it operates on the live app (register, integrate, restart) and
re-verifies each effect survived (CP7 durability).

- [redu-medium-a](redu-medium-a/) - deploy, mutate:register, integrate, restart, terminal durability re-verify (n=1; fresh post-fix run, supersedes the 08-31 baseline)
- [aws-medium-a](aws-medium-a/) - deploy, mutate:register, integrate, restart, terminal durability re-verify (n=10)
- [gcp-medium-a](gcp-medium-a/) - deploy, mutate:register, integrate, restart, terminal durability re-verify (n=9; run05 resolved by the second-site naming fix)

Each easy table is `run | t1 (s) | platform (s) | agent (s) | steps | tokens | agent $ | fixed $/mo | $/mo @ 10k
/ 500k / 10M req`. `fixed $/mo` is the flat floor: for a VM it stays flat at any traffic; a serverless front
rises with traffic. The cost line-item audit is in `COST-COVERAGE-AUDIT.md`; every transcript is credential-
and infrastructure-redacted (`acspeed sessions`, residue 0).
