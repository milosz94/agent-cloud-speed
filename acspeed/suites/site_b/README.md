# acspeed standard site B (Medium tier)

A small, real Node.js/Express app that stands in as the **second website** in the Medium tier: the
site the agent deploys and then wires umami tracking into. It replaces the old single static HTML
file so that `deploy-site-b` exercises a real application deploy (a container with a run step) on
every cloud, the way a real second service would, rather than a trivial file drop.

## Properties that make it a fair fixture

- **Standardized and constant.** Byte-identical for every run and every cloud. No per-run values are
  baked in; the unique per-run sentinel is only a URL path that a visitor hits.
- **Serves every path.** The catch-all route returns the same page for `/`, a deep link, or the
  per-run sentinel path, so the app never needs to know the sentinel.
- **Ships with no analytics.** `public/index.html` carries no tracking. The agent adds the umami
  `<script>` into `<head>` during the `integrate` operation, so `deploy-site-b` never telegraphs the
  integration that follows.
- **Source only, no deploy prescription.** Reads `$PORT` and starts with `npm start`. The fixture
  ships only source code (no Dockerfile, no manifest); how it is deployed (a VM process, a container,
  a serverless service) is the agent's own choice, which is part of what the tier measures.

## Run it locally

    npm install
    npm start
    # open http://localhost:3000  (or PORT=4173 npm start)

`GET /healthz` returns `ok`.
