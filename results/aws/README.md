# aws

| tier | floor-ratio | 95% CI | n | F_C (s) | bracket (s) | gold |
|:-----|------------:|:-------|--:|--------:|:------------|:-----|
| [easy](aws-easy/) | 6.0x | [4.5, 7.5] | 10 | 152.6 | [152.6, 256.3] = x1.68 | provision-deploy/1.1.0 |

**What this measures.** An AI agent deploys the same web app and its database to this cloud, 10 times,
each time from scratch. Each run is timed from outside: **t1** = seconds from task start until the app's
public URL first answers (polled externally every few seconds); a separate off-clock check then confirms
the page really was the app and not an error page. Only runs that pass the publication checks in
[PLAYBOOK.md](../PLAYBOOK.md) are counted. The per-run numbers behind this row: [aws-easy](aws-easy/).

**How to read the row.**
- **floor-ratio** = average t1 divided by F_C: how many times slower the average deploy was than the
  fastest this cloud has shown it can go.
- **95% CI**: the plausible range for that ratio given only 10 runs (bootstrap: the calculation is
  repeated over resampled runs).
- **n**: number of counted runs.
- **F_C**: the floor. Every run's time splits into platform time (waiting for the cloud to provision and
  boot things) and agent time (the AI reading, deciding, running commands). F_C is the smallest platform
  time observed in any run here: even an instantaneous agent could not have finished below it.
- **bracket**: the true best-possible time on this cloud is unknown, but it must sit between F_C and the
  best complete run actually observed; x1.68 says how wide that window still is. The ratio is therefore
  an interval, [average / best run, average / F_C], not a single number.
- **gold**: version of the reference definition these numbers were computed under; compare ratios only
  within the same version.
