# Benchmark publication terms of the measured providers

Read 2026-09-07. This benchmark names the clouds it measures, so this file records what each provider's
own terms say about publishing benchmark results, and how this project meets them. Quotes are verbatim
from the primary source; each is dated because all three documents change without notice.

Every one of the three **permits** publication. None requires prior consent for the services measured
here. What each asks for is, in substance, the same thing: publish enough that someone else can repeat
it, and accept being benchmarked in return.

---

## Amazon Web Services

Source: [AWS Service Terms](https://aws.amazon.com/service-terms/), section 1.8, read 2026-09-07.

> If you perform or disclose, or direct or permit any third party to perform or disclose, any Benchmark
> of any of the Services, you (i) will include in any disclosure, and will disclose to us, all
> information necessary to replicate such Benchmark, and (ii) agree that we may perform and disclose the
> results of Benchmarks of your products or services, irrespective of any restrictions on Benchmarks in
> the terms governing your products or services.

**How this project meets it.** (i) The full protocol, the harness, the per-run records and the
credential- and infrastructure-redacted agent transcripts are published in this repository, which is
what "all information necessary to replicate" asks for. AWS is notified separately, because 1.8 requires
disclosure **to AWS**, not only to the public. (ii) Accepted without reservation.

## Microsoft Azure

Source: [Microsoft Product Terms for Online Services](https://www.microsoft.com/licensing/terms/product/ForOnlineServices/MCA),
heading "Competitive Benchmarking", read 2026-09-07. The Online Subscription Agreement itself carries no
benchmark clause; this one lives in the Product Terms.

> If Customer offers a product or service competitive to an Online Service and discloses, directly or
> through third parties, any benchmarks or comparative tests or evaluations (each, a "Benchmark") of any
> Online Service, Customer will, upon request from Microsoft, provide: (a) all information necessary to
> replicate such Benchmark; and (b) access to Customer's competitive products and services for
> Microsoft, directly or through third parties, to perform and disclose Benchmarks.

**This clause applies to us.** It is conditioned on the customer offering a competing service, and the
authors operate one. (a) is already public in this repository. (b) is satisfied by the competing service
being open self-serve: Microsoft can obtain access the same way any member of the public does, without
asking.

## Google Cloud

Source: [Google Cloud Platform Terms of Service](https://cloud.google.com/terms) and the
[Service Specific Terms](https://cloud.google.com/terms/service-terms), read 2026-09-07.

Google permits benchmark tests and public disclosure of the results where the disclosure carries all
information necessary to replicate the tests, and where the customer allows Google to benchmark the
customer's publicly available products and services and disclose those results. Two further conditions
matter to any reader repeating this work:

* A customer may not conduct or disclose benchmark results **on behalf of a hyperscale public cloud
  provider** without Google's prior written consent. This study is published on its own behalf.
* Some services are excluded outright or need prior written consent (Cloud NGFW Enterprise, Cloud IDS,
  and the SecOps services). **None of them is used here**: the measured runs use Cloud Run, Cloud SQL and
  the surrounding networking the agent chose.

**Completeness caveat.** The AWS and Microsoft clauses above were read from the primary documents and are
quoted word for word. The Google terms were long enough that the retrieved copy was truncated, so the
Google summary is a paraphrase rather than a verbatim clause. Anyone relying on it should read the
current source before publishing their own results.

---

## What a reader should take from this

The condition common to all three is **replicability of the disclosure**, which an open benchmark
satisfies by construction. Publishing the protocol and the runs is not merely good practice here; it is
the thing the providers' own terms ask for.

The reciprocal half is equally real: by naming these providers, this project accepts that they may
benchmark the authors' own cloud and publish the results.

Terms change. These readings are dated 2026-09-07 and are not legal advice.
