# aws

| tier | floor-ratio | 95% CI | n | F_C (s) | bracket (s) | gold |
|:-----|------------:|:-------|--:|--------:|:------------|:-----|
| [easy](aws-easy/) | 6.0x | [4.5, 7.5] | 10 | 152.6 | [152.6, 256.3] = x1.68 | provision-deploy/1.1.0 |
| [medium-a](aws-medium-a/) | 7.7x | [6.0, 9.7] | 10 | 110.3 | [110.3, 267.5] = x2.425 | provision-deploy/1.1.0 |

- **floor-ratio**: mean deploy time / F_C; deploy time = seconds until the app's public URL first answers (polled externally)
- **95% CI**: bootstrap over the n runs
- **F_C**: fastest platform-only time observed (cloud provisioning and boot waits; no agent can go below it)
- **bracket**: [F_C, best observed run]; the true optimum lies inside, x = its width
- **gold**: version of the reference definition; compare ratios only within one version
