# Reproducibility to-do before the benchmark is published open-source

Found 2026-09-07 while checking that "any provider can send a PR on their own adapter" is actually
true. It is not yet. None of this blocks the paper; all of it blocks a credible open release, because
every provider's terms condition publication on the disclosure carrying enough to replicate the work
(`BENCHMARK-TERMS.md`).

Milos owns this and will do a repo pass; this file is the list so it is not rediscovered.

## 1. Config lives outside the repo

`git ls-files | grep mcp.json` returns nothing. The four MCP configs sit in
`/home/milos/Desktop/research_paper_data/_config/`, and `autorun.ADAPTERS` points at them by absolute
path. So does `DATA` generally: the capability reference vector and the workload spec are there too.

A reader who clones this repo cannot run it, and a provider cannot send a PR against the file that most
determines their result. Checked for secrets: aws, azure and redu configs hold none; the gcp one holds
`GOOGLE_CLOUD_PROJECT=redu-425516` and a region, a project id rather than a credential. All four are
committable as they stand.

Absolute paths to `/home/milos/...` are baked into `autorun.py` (`DATA`), the adapter table and
`reprice_aws.py` (`STAGING`, `REPO`). They need to become repo-relative or configurable.

## 2. The agent's toolchain is unpinned again

    aws    mcp-proxy-for-aws@latest
    azure  @azure/mcp@latest

This already broke the study once: `@latest` dropped `aws___call_aws` and the cost path priced nothing
for three days. An unpinned agent tool is an uncontrolled instrument, and two of four are unpinned now.

## 3. No run record says which tool versions ran

Searched a published run record for any version or tool key: there is none. The model is recorded
(`claude-opus-5`); the MCP server versions, the cloud CLI versions and the adapter revision are not. So
the 94 published runs may span tool versions with nothing to say which, and a provider auditing their
own row cannot tell what they are auditing.

Minimum fix: resolve and record the actual versions per run at launch, alongside `measured_at`.

## 4. The staging tree is outside the repo too

`results/` holds the published tables and the redacted transcripts, which is right. But
`paper_tables.py` resolves every published row through run records in
`/home/milos/Desktop/tests_lib/umami/acspeed-results/`, which is not published. A reader can see the
tables and the transcripts but cannot re-derive a single cell.

## 5. Then, and only then, the PR invitation

Once the above are true, `BENCHMARK-TERMS.md` should carry the section it currently cannot: a provider
who thinks their adapter misrepresents them can open a PR against their own
`acspeed/adapters/<cloud>_*.py`, their entry in `acspeed/adapters/profiles.py`, and their MCP config, and
the maintainers re-run the affected cells. That is the fairest offer this benchmark can make, and it is
only credible when the files are actually in the repo.
