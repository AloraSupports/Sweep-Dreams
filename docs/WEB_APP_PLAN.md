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
5. Copy the two AxisCare reports into the container's data volume (`docker compose cp caregivers.csv app:/data/axiscare/`; the folder exists from the first start), until the AxisCare API backend exists.
6. Open the site, sign in, run a sweep with "prepare at most 2".

## Deploy runbook (exact commands)

Do steps A–C in the Google Cloud console or Cloud Shell, D on the VM. Nothing here needs
to be typed on a staff computer.

**A. Project (once).** Console → New project `alora-sweep` → attach billing. Accept the
HIPAA BAA: console → Compliance (or Workspace admin → Account → Legal and compliance →
Google Cloud BAA). Enable the Compute Engine API.

**B. VM and address (Cloud Shell).**
```
gcloud config set project alora-sweep
gcloud compute addresses create sweep-ip --region us-central1
gcloud compute instances create sweep-vm --zone us-central1-a --machine-type e2-small \
  --image-family debian-12 --image-project debian-cloud --boot-disk-size 20GB \
  --address sweep-ip --tags http-server,https-server --shielded-secure-boot
gcloud compute addresses describe sweep-ip --region us-central1 --format 'value(address)'
```
If the default network has no `default-allow-http` / `default-allow-https` rules:
```
gcloud compute firewall-rules create allow-web --allow tcp:80,tcp:443 --target-tags http-server,https-server
```

**C. DNS and sign-in.** Add an A record `sweep` → the address from step B, in the DNS
of alorasupports.com (Google Workspace → Domains, or the registrar). Then console → APIs &
Services → OAuth consent screen → *Internal* → Credentials → Create OAuth client ID → *Web
application*, authorized redirect URI `https://sweep.alorasupports.com/auth/callback`.
Keep the client ID and secret for the `.env` file.

**D. On the VM.** From your computer, copy the script over and sign in:
```
gcloud compute scp deploy/setup-vm.sh sweep-vm:~ --zone us-central1-a
gcloud compute ssh sweep-vm --zone us-central1-a
bash setup-vm.sh
```
The script installs Docker, makes a read-only deploy key for the private repo (it pauses
while you add the key in GitHub), clones, creates `deploy/.env`, and probes whether the
VM can reach the portal and the state form. Then:
```
nano ~/sweep/deploy/.env            # fill every line, keeping the quotes; the session secret is any long random string
cd ~/sweep/deploy && docker compose up -d --build
docker compose logs -f app          # watch the first start
docker compose cp caregivers.csv app:/data/axiscare/
docker compose cp authorizations.csv app:/data/axiscare/
docker compose exec app alora-evv check
```
Open `https://sweep.alorasupports.com`, sign in with an @alorasupports.com account, and run
a sweep with "prepare at most 1". The first sweep is the cloud-IP portal login test.

**Updating later:** `cd ~/sweep && git pull && cd deploy && docker compose up -d --build`.

**Backups:** the only state is the `data` volume: the ledger (visit IDs and statuses),
the AxisCare reports (client Medicaid IDs) and the saved portal login. The ledger is the
only part worth keeping; the reports come from AxisCare and the login is re-created.
Encrypt the archive and leave nothing readable on the host:
```
umask 077
docker run --rm -v deploy_data:/data -v "$PWD":/out debian \
  tar czf /out/ledger.tgz -C /data ledger.sqlite3
gpg -c ledger.tgz && shred -u ledger.tgz        # keep ledger.tgz.gpg somewhere safe
```

**Hardening left for later:** the app container runs as root (the Playwright base image
has a `pwuser` account; switching needs a `chown` of `/data` and a test build), and the
saved portal login can be turned off with `portal.save_session: false` in settings.yaml
if a fresh login per sweep is preferred over a token on the volume.

## Known risks to test early

- **Portal login from a cloud IP.** If Mobile Caregiver+ blocks or challenges it, ask the vendor about allow-listing the VM's static IP. Fallback: run the portal read on an office computer and upload the export.
- **Form prefill.** Decides whether the Chrome extension is needed.
- **Sessions across restarts.** The portal session file lives on the data volume, so it survives restarts.

## Later

- Billing inbox watcher (Gmail API, service account with domain-wide delegation limited to the billing mailbox) → Accepted / Rejected / Educational per visit, educational log per caregiver, shown on the dashboard.
- AxisCare API backend.
- Scheduled morning sweep (cron in the container) so the queue is ready before staff sign in.
