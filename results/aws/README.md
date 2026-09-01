# aws: Easy-tier umami (2026-08-29)

n=10 (run01-10). Architecture: Fargate or EC2 + ALB + a database (managed RDS, or Postgres in-task/on-VM) +
public IPv4, all fixed / hourly-billed. What the agent provisions varies run to run, so the cost does too.

| run | t1 (s) | platform (s) | agent (s) | steps | tokens | agent $ | fixed $/mo | $/mo @ 10k req | $/mo @ 500k req | $/mo @ 10M req |
|----:|-------:|-------------:|----------:|------:|-------:|--------:|-----------:|---------------:|----------------:|---------------:|
| 1 | 875.3 | 617.1 | 240.8 | 31 | 24923 | $2.05 | $48.43 | $48.43 | $48.43 | $48.43 |
| 2 | 956.7 | 719.4 | 224.5 | 31 | 22054 | $1.65 | $48.43 | $48.43 | $48.43 | $48.43 |
| 3 | 1045.8 | 682.7 | 353.7 | 54 | 29706 | $2.84 | $66.45 | $66.45 | $66.45 | $66.45 |
| 4 | 1664.6 | 270.2 | 239.3 | 32 | 22249 | $1.60 | $48.43 | $48.43 | $48.43 | $48.43 |
| 5 | 626.3 | 207.3 | 408.8 | 79 | 43255 | $4.54 | $66.45 | $66.45 | $66.45 | $66.45 |
| 6 | 1169.9 | 790.9 | 369.2 | 39 | 34535 | $2.55 | $48.43 | $48.43 | $48.43 | $48.43 |
| 7 | 938.1 | 517.1 | 409.9 | 60 | 35514 | $3.31 | $48.43 | $48.43 | $48.43 | $48.43 |
| 8 | 256.3 | 146.2 | 100.7 | 12 | 10908 | $0.72 | $55.71 | $55.71 | $55.71 | $55.71 |
| 9 | 565.3 | 235.1 | 320.2 | 34 | 32593 | $2.15 | $33.21 | $33.21 | $33.21 | $33.21 |
| 10 | 1000.8 | 802.8 | 188.4 | 35 | 19776 | $1.65 | $66.45 | $66.45 | $66.45 | $66.45 |

`fixed $/mo` is the flat price of the whole footprint; being fixed / hourly-billed, each run's bill stays flat
at any traffic (the three columns are equal). Cost ranges **$33-66/mo** across runs, driven by the agent's
choices: Fargate task size vs EC2, and a managed RDS vs a Postgres running in-task / on the VM (its compute is
then already inside the Fargate/EC2 line). Redacted transcripts in `sessions/`.
