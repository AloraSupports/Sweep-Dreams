# Web version: how it runs

Goal: staff open a link, sign in, click Run sweep, review each visit, open the state
form already filled, complete the captcha, and submit. Nothing to install.

## Pieces

| Piece | Choice | Why |
|---|---|---|
| Hosting | One small Google Cloud VM running Docker (`deploy/`) | Google signs a HIPAA BAA; you're already on Google Workspace; the app needs a browser (Playwright) and a small database, which fit a VM better than serverless |
| Domain | `sweep.alorasupports.com` (a subdomain you already own) | No purchase; looks official. A separate brand domain works the same way — set `APP_DOMAIN` |
| HTTPS | Caddy, automatic certificates | Zero maintenance |
| Sign-in | Google sign-in restricted to `@alorasupports.com` | Staff use the passwords they have; turn on 2-step verification in Workspace admin and it's enforced here too. No home-made password/2FA to maintain |
| Secrets | `.env` on the VM (later Secret Manager) | Read by `credentials.py` as `ALORA_EVV_*` environment variables |
| Portal read | Headless Chromium on the VM | Same code as the desktop tool |
| Opening the form | Prefilled link (`form.prefill_url`) → **confirm with a live test** | If the state's form accepts URL prefill, no browser automation is needed on the reviewer's side. If not: a small Chrome extension that fills the form from the review page |
| Data at rest | Ledger (visit IDs, statuses, decisions) on the VM's disk | Prepared visits with client names are kept in memory for the current run only |

## Setup checklist

1. Google Cloud: create a project, attach billing, accept the BAA (Cloud console → Compliance / or via your Workspace admin), create an `e2-small` VM (Debian) with a static external IP, allow HTTP/HTTPS.
2. DNS: add an A record `sweep` → the VM's IP in your domain's DNS (Google Workspace or wherever the domain lives).
3. Google sign-in: APIs & Services → OAuth consent screen (Internal) → Credentials → OAuth client ID (Web application), authorized redirect URI `https://sweep.alorasupports.com/auth/callback`. Put the ID and secret in `.env`.
4. On the VM: install Docker, clone the repo, `cp deploy/.env.example deploy/.env`, fill it in, `chmod 600 deploy/.env`, then `cd deploy && docker compose up -d --build`.
5. Copy the two AxisCare reports into the container's data volume (`docker compose cp caregivers.csv app:/data/axiscare/`), until the AxisCare API backend exists.
6. Open the site, sign in, run a sweep with "prepare at most 2".

## Known risks to test early

- **Portal login from a cloud IP.** If Mobile Caregiver+ blocks or challenges it, ask the vendor about allow-listing the VM's static IP. Fallback: run the portal read on an office computer and upload the export.
- **Form prefill.** Decides whether the Chrome extension is needed.
- **Sessions across restarts.** The portal session file lives on the data volume, so it survives restarts.

## Later

- Billing inbox watcher (Gmail API, service account with domain-wide delegation limited to the billing mailbox) → Accepted / Rejected / Educational per visit, educational log per caregiver, shown on the dashboard.
- AxisCare API backend.
- Scheduled morning sweep (cron in the container) so the queue is ready before staff sign in.
