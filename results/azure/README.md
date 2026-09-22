# azure

| tier | floor-ratio | 95% CI | n | F_C (s) | bracket (s) | gold |
|:-----|------------:|:-------|--:|--------:|:------------|:-----|
| [easy](azure-easy/) | 1.8x | [1.6, 2.1] | 10 | 984.0 | [984.0, 1309.5] = x1.331 | provision-deploy/1.1.0 |
| [medium-a](azure-medium-a/) | 5.5x | [4.4, 6.6] | 10 | 314.8 | [314.8, 728.4] = x2.314 | provision-deploy/1.1.0 |
| [medium-b](azure-medium-b/) | 2.7x | [2.2, 3.1] | 10 | 573.5 | [573.5, 873.8] = x1.524 | provision-deploy/1.1.0 |

- **floor-ratio**: mean deploy time / F_C; deploy time = the deploy leg's own critical path, which on the Easy tier equals the externally polled time-to-serving and on the Medium tiers ends before it
- **95% CI**: bootstrap over the n runs
- **F_C**: fastest platform-only time observed (cloud provisioning and boot waits; no agent can go below it)
- **bracket**: [F_C, best observed run]; the true optimum lies inside, x = its width
- **gold**: version of the reference definition; compare ratios only within one version
