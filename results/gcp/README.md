# gcp

| tier | floor-ratio | 95% CI | n | F_C (s) | bracket (s) | gold |
|:-----|------------:|:-------|--:|--------:|:------------|:-----|
| [easy](gcp-easy/) | 3.0x | [2.5, 3.7] | 12 | 264.2 | [264.2, 478.7] = x1.812 | provision-deploy/1.1.0 |
| [medium-a](gcp-medium-a/) | 2.6x | [2.5, 2.7] | 10 | 161.6 | [161.6, 330.6] = x2.046 | provision-deploy/1.1.0 |
| [medium-b](gcp-medium-b/) | 4.6x | [4.0, 5.3] | 10 | 85.1 | [85.1, 292.1] = x3.432 | provision-deploy/1.1.0 |

- **floor-ratio**: mean deploy time / F_C; deploy time = the deploy leg's own critical path, which on the Easy tier equals the externally polled time-to-serving and on the Medium tiers ends before it
- **95% CI**: bootstrap over the n runs
- **F_C**: fastest platform-only time observed (cloud provisioning and boot waits; no agent can go below it)
- **bracket**: [F_C, best observed run]; the true optimum lies inside, x = its width
- **gold**: version of the reference definition; compare ratios only within one version
