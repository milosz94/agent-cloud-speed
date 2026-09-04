# aws

| tier | floor-ratio | 95% CI | n | F_C (s) | bracket (s) | gold |
|:-----|------------:|:-------|--:|--------:|:------------|:-----|
| [easy](aws-easy/) | 6.0x | [4.5, 7.5] | 10 | 152.6 | [152.6, 256.3] = x1.68 | provision-deploy/1.1.0 |

- **floor-ratio**: mean deploy time / F_C; deploy time = seconds until the app's public URL first answers (polled externally)
- **F_C**: fastest platform-only time observed (cloud provisioning and boot waits; no agent can go below it)
- **95% CI**: bootstrap over the n runs
- **bracket**: [F_C, best observed run]; the true optimum lies inside, x = its width
- **gold**: version of the reference definition; compare ratios only within one version
