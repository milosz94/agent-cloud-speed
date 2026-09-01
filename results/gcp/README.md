# gcp: Easy-tier umami (2026-08-29)

n=10 (ten runs through the corrected cost tool). Architecture: Cloud Run + Cloud SQL + public IPv4
(serverless: a fixed floor that rises with traffic).

| run | t1 (s) | platform (s) | agent (s) | steps | tokens | agent $ | fixed $/mo | $/mo @ 10k req | $/mo @ 500k req | $/mo @ 10M req |
|----:|-------:|-------------:|----------:|------:|-------:|--------:|-----------:|---------------:|----------------:|---------------:|
| 1 | 484.1 | 315.0 | 133.4 | 20 | 13388 | $1.18 | $29.26 | $29.28 | $30.72 | $58.51 |
| 2 | 1456.5 | 143.6 | 129.7 | 18 | 10485 | $0.94 | $29.26 | $29.28 | $30.72 | $58.51 |
| 3 | 797.5 | 162.2 | 137.1 | 19 | 11652 | $0.98 | $68.67 | $68.67 | $68.67 | $68.67 |
| 4 | 507.1 | 311.7 | 162.0 | 21 | 13348 | $1.10 | $29.26 | $29.28 | $30.72 | $58.51 |
| 5 | 730.0 | 119.8 | 172.9 | 32 | 19142 | $1.85 | $29.26 | $29.28 | $30.72 | $58.51 |
| 6 | 754.4 | 77.9 | 112.9 | 20 | 9443 | $0.93 | $29.26 | $29.28 | $30.72 | $58.51 |
| 7 | 795.7 | 102.7 | 150.2 | 23 | 14141 | $1.49 | $29.26 | $29.28 | $30.72 | $58.51 |
| 8 | 720.9 | 92.2 | 149.7 | 23 | 12193 | $1.14 | $29.26 | $29.28 | $30.72 | $58.51 |
| 9 | 850.9 | 152.0 | 196.8 | 28 | 18117 | $1.60 | $29.26 | $29.28 | $30.72 | $58.51 |
| 10 | 478.7 | 255.3 | 190.0 | 29 | 17574 | $1.51 | $29.26 | $29.28 | $30.72 | $58.51 |

`fixed $/mo` is the always-on floor (Cloud Run min-instance + Cloud SQL + IP). Nine runs are request-based
serverless, so the bill rises with traffic (**$29.28/mo at 10k up to $58.51/mo at 10M req**). Run 3 used
instance-based billing (`--no-cpu-throttling`, the agent's choice), which is a flat **$68.67/mo** at any
traffic. Redacted transcripts in `sessions/`.
