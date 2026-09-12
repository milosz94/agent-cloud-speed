# The application under test

This is the app every published run in `results/` deployed: umami, pinned by image digest.

`acspeed-run --suite umami-medium` (and the other umami suites) materializes this folder into your
working directory automatically when it is not already there, generating a fresh `APP_SECRET` into a
local `.env`. You do not need to create anything by hand.

**Do not change the digest** if you intend to compare against the published cells. The paper's
comparability rests on every cell deploying the same image; a different build is a different
measurement.

`DATABASE_URL` is supplied by the deploy step, which provisions a managed Postgres on the target
cloud. `APP_SECRET` is yours and is generated locally; it is never committed and never shared.
