# redu

| tier | floor-ratio | 95% CI | n | F_C (s) | bracket (s) | gold |
|:-----|------------:|:-------|--:|--------:|:------------|:-----|
| [easy](redu-easy/) | 1.7x | [1.7, 1.8] | 10 | 134.6 | [134.6, 214.4] = x1.593 | provision-deploy/1.1.0 |
| [medium-a](redu-medium-a/) | 2.4x | [2.0, 2.9] | 10 | 123.7 | [123.7, 203.3] = x1.643 | provision-deploy/1.1.0 |
| [medium-b](redu-medium-b/) | 4.5x | [4.2, 4.7] | 10 | 67.9 | [67.9, 263.1] = x3.875 | provision-deploy/1.1.0 |

- **floor-ratio**: mean deploy time / F_C; deploy time = seconds until the app's public URL first answers (polled externally)
- **95% CI**: bootstrap over the n runs
- **F_C**: fastest platform-only time observed (cloud provisioning and boot waits; no agent can go below it)
- **bracket**: [F_C, best observed run]; the true optimum lies inside, x = its width
- **gold**: version of the reference definition; compare ratios only within one version
