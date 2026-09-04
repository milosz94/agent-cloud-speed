# gcp

| tier | floor-ratio | 95% CI | n | F_C (s) | bracket (s) | selection excess | gold |
|:-----|------------:|:--------|--:|--------:|:------------|:-----------------|:-----|
| [easy](gcp-easy/) | 3.0x | [2.5, 3.7] | 12 | 264.2 | [264.2, 478.7] = x1.812 | 0 (1-op suite) | provision-deploy/1.1.0 |

floor-ratio = M / F_C, where M is the mean time-to-serving (t1) over the cell's fair runs and F_C is the minimum observed critical-platform time among them. The competitive ratio is the interval [M / best-achieved, M / F_C], never a point.
