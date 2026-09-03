# Deploying to Render (one-click)

The easiest way to get a private tret instance on the internet, no terminal
required. Render reads [`render.yaml`](../render.yaml) from this repository and
creates everything tret needs: the app itself, a managed Postgres database,
and a persistent disk for uploaded documents. LLM provider keys are entered
later, in the app — you do not need one to deploy.

One rule to know up front: tret runs as a **single instance**. The blueprint
enforces this, and the attached disk makes Render enforce it too. Do not raise
the instance count later — parts of the app (live run streams) work through
in-process state that cannot be shared across copies (see
docs/architecture.md).

## The button

<!-- README-SNIPPET: copy the block below into README.md verbatim -->

```markdown
[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/kith-ai-lab/tret)
```

<!-- /README-SNIPPET -->

Deploying from your own fork instead? Same snippet, with the `repo=` URL
swapped for your fork's GitHub URL. A fork is worth having if you want to
control when updates reach your instance — pushes to the deployed repo are
what trigger redeploys.

## What happens when you click it

1. **Sign in to Render** (or create a free account — the button takes you
   there). You will need to add a payment method: the plans this blueprint
   uses are paid, because Render's free tier has no persistent disk and its
   free database is deleted after 30 days. Costs are below.
2. **Render shows the blueprint** — the `tret` web service, the `tret-db`
   database, and one question it needs answered:
   - **`TRET_ADMIN_EMAIL`** — the email address you will log in with. It
     becomes the first (admin) account.
3. **Click Apply.** Render builds the app from source; the first build takes
   around 5–10 minutes. When the `tret` service shows **Live**, your
   instance is up at `https://tret-XXXX.onrender.com` (the exact URL is on
   the service page).

Everything else is decided for you: a random secret key and a random admin
password are generated at deploy time, and production mode (already baked
into the image) refuses to boot until both exist — so a half-configured
instance fails loudly instead of running insecurely.

## First login

1. Find your admin password: Render dashboard → the **tret** service →
   **Environment** tab → reveal **`TRET_ADMIN_PASSWORD`**. That value (and
   the email you entered at deploy time) is your login.
2. Open your instance URL and sign in.
3. **Settings → Team**: create accounts for teammates (analyst / approver /
   admin). Each password is shown once at creation — send it over a secure
   channel.

## Add a provider key

tret talks to LLM providers with keys you control. After logging in:

1. Go to **Settings → Provider keys**.
2. Paste a key for at least one provider (OpenRouter is the simplest single
   key; Anthropic and Moonshot are also supported).
3. Keys are stored encrypted and never shown again after saving.

That's it — runs will work from here.

## Custom domain (optional)

Render dashboard → the **tret** service → **Settings → Custom Domains** →
add your domain and create the DNS record Render shows you (a CNAME).
HTTPS certificates are automatic. The default `.onrender.com` URL keeps
working either way.

## Cost expectations

| Item | Plan | Approx. monthly |
| --- | --- | --- |
| Web service | `starter` (512MB) | $7 |
| Postgres | `basic-256mb` | $6 |
| Disk (uploads) | 1GB | well under $1 |

Roughly **$13–14/month**, plus whatever you spend with your LLM provider.
Why not free: Render's free web instances cannot attach a disk (uploaded
documents would vanish on every restart) and free Postgres databases are
deleted after 30 days. These are the cheapest plans that keep your data.

Two upgrades you might want later, both from the dashboard with no config
changes:

- **More memory** — if the service runs out of memory (PDF export is the
  heavy part), move the web service to the `standard` plan. Keep the
  instance count at 1.
- **More disk** — the disk can be grown any time (never shrunk), so it
  starts at 1GB.

## Good to know

- **Brief downtime on updates.** Because the service has a disk attached,
  Render stops the old instance before starting the new one — expect your
  instance to be unavailable for a minute or two during each deploy.
- **Upgrades take care of the database.** On boot tret migrates its own
  schema and re-runs its idempotent seed, so redeploying a newer version
  keeps all data (docs/upgrading.md has the details and recovery commands).
- **Back up before big upgrades.** Render's paid Postgres plans include
  daily backups; taking a manual one from the database's page before
  upgrading tret is cheap insurance (see docs/hardening.md §8).
- **Don't rotate `TRET_SECRET_KEY`.** It encrypts the provider keys you
  enter in Settings; changing it logs everyone out and makes those stored
  keys unreadable (you would re-enter them).
- The health check hits `/api/healthz`; logs are on the service's **Logs**
  tab if something looks wrong.

The full production checklist — what is and is not guaranteed — lives in
[hardening.md](hardening.md).
