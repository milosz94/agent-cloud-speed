# aws

| tier | floor-ratio | 95% CI | n | F_C (s) | bracket (s) | gold |
|:-----|------------:|:-------|--:|--------:|:------------|:-----|
| [easy](aws-easy/) | 6.0x | [4.5, 7.5] | 10 | 152.6 | [152.6, 256.3] = x1.68 | provision-deploy/1.1.0 |
| [medium-a](aws-medium-a/) | 7.0x | [5.0, 9.3] | 10 | 109.8 | [109.8, 226.6] = x2.064 | provision-deploy/1.1.0 |
| [medium-b](aws-medium-b/) | 5.7x | [4.5, 6.8] | 12 | 158.3 | [158.3, 427.3] = x2.699 | provision-deploy/1.1.0 |

- **floor-ratio**: mean deploy time / F_C; deploy time = the deploy leg's own critical path, which on the Easy tier equals the externally polled time-to-serving and on the Medium tiers ends before it
- **95% CI**: bootstrap over the n runs
- **F_C**: fastest platform-only time observed (cloud provisioning and boot waits; no agent can go below it)
- **bracket**: [F_C, best observed run]; the true optimum lies inside, x = its width
- **gold**: version of the reference definition; compare ratios only within one version
