# redu

**Not part of the paper.** The paper this release accompanies reports the `aws`, `gcp` and `azure` cells
only. This cloud's runs are published here, but they are not part of that paper.

| tier | floor-ratio | 95% CI | n | F_C (s) | bracket (s) | gold |
|:-----|------------:|:-------|--:|--------:|:------------|:-----|
| [easy](redu-easy/) | 1.7x | [1.7, 1.8] | 10 | 134.6 | [134.6, 214.4] = x1.593 | provision-deploy/1.1.0 |
| [medium-a](redu-medium-a/) | 2.4x | [2.0, 2.9] | 10 | 123.7 | [123.7, 203.3] = x1.643 | provision-deploy/1.1.0 |
| [medium-b](redu-medium-b/) | 4.5x | [4.2, 4.7] | 10 | 67.9 | [67.9, 263.1] = x3.875 | provision-deploy/1.1.0 |

- **floor-ratio**: mean deploy time / F_C; deploy time = the deploy leg's own critical path, which on the Easy tier equals the externally polled time-to-serving and on the Medium tiers ends before it
- **95% CI**: bootstrap over the n runs
- **F_C**: fastest platform-only time observed (cloud provisioning and boot waits) over this cell's clean runs: a sample minimum that later runs can lower, not a proved lower bound
- **bracket**: [F_C, best observed run], an interval between two measured references, not a proved bracket on the optimum; x = its width
- **gold**: version of the reference definition; compare ratios only within one version
