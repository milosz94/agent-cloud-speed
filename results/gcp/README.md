# gcp

| tier | floor-ratio | 95% CI | n | F_C (s) | bracket (s) | gold |
|:-----|------------:|:-------|--:|--------:|:------------|:-----|
| [easy](gcp-easy/) | 3.0x | [2.5, 3.7] | 12 | 264.2 | [264.2, 478.7] = x1.812 | provision-deploy/1.1.0 |

- **floor-ratio**: mean deploy time / F_C; deploy time = seconds until the app's public URL first answers (polled externally)
- **F_C**: fastest platform-only time observed (cloud provisioning and boot waits; no agent can go below it)
- **95% CI**: bootstrap over the n runs
- **bracket**: [F_C, best observed run]; the true optimum lies inside, x = its width
- **gold**: version of the reference definition; compare ratios only within one version
