# Alora EVV adjustment assistant

Reads EVV errors from Mobile Caregiver+, checks each visit against AxisCare, and
opens the state's adjustment request form already filled in, one browser tab per
visit. A person reviews each tab, completes the captcha, and clicks Submit.
**The app never submits anything.** Runs on Mac and Windows.

## What it does on each run

1. Signs in to the Mobile Caregiver+ dashboard (payer NDHH) and, for each provider
   agency (Alora Supports NE, First Choice), reads the Work List, each visit's
   claim-matching errors, and the export.
2. For each visit needing adjustment, works out the error type (missing clock-in,
   missing clock-out, wrong location, …) using `config/rules.yaml`.
3. Looks up the caregiver's NPI and the client's authorization in AxisCare, checks the
   NPI's check digit, and **flags any claim whose authorization doesn't match AxisCare**.
4. Holds back anything unclear, with the reason, instead of guessing.
5. Opens the filled forms for review, and records each one you submit so it isn't
   prepared again while the state is processing it.

## Setup (once per computer)

You need **Python 3.12** (python.org) and **Google Chrome**.

**Mac** (Terminal, in the project folder):
```
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

**Windows** (PowerShell, in the project folder):
```
py -3.12 -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
```
If PowerShell refuses to run the activate script, run
`Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` once, then try again.

**Logins** go into the Mac Keychain or Windows Credential Manager. Typing is hidden
and nothing is saved to files or shell history:
```
alora-evv credentials set portal_username
alora-evv credentials set portal_password
```
On a Windows computer that ran Yitzi's version, `alora-evv credentials import-env`
copies the old `setx` values over and tells you how to delete the plain-text copies.

**AxisCare data.** Until AxisCare API access is set up, the app reads two AxisCare
report exports. Run `alora-evv where` to find the data folder, create an `axiscare`
folder inside it, and save:
- `caregivers.csv`: caregiver first name, last name and NPI
- `authorizations.csv`: client Medicaid ID, service code, authorization number, start and end dates (and program, if available)

Then open `config/settings.yaml` → `axiscare.export` and set the column names to match
the real reports. `alora-evv check` tells you what's missing.

## Daily use

```
alora-evv sweep --review
```
The terminal lists what was prepared and what was held (and why). A report is saved
in the data folder's `reports` folder. The report has no client names or Medicaid IDs.

**First live run:** use `alora-evv sweep --review --show-portal --max 2` so you can
watch the portal steps and review only two forms.

**Held visits** that need a judgment call show a `decide` hint:

| Command | Effect on the next sweep |
|---|---|
| `alora-evv decide 1234567890 AUTH` | File as an authorization exception (Reason 120) using AxisCare's authorization |
| `alora-evv decide 1234567890 VLOC` / `VVER` / `VSTR` | Treat as that error type despite other portal codes |
| `alora-evv decide 1234567890 KNOWN` | Fill known info only; you choose Critical Error and Reason Code |
| `alora-evv decide 1234567890 SKIP` | Leave it alone |
| `alora-evv decide 1234567890 --clear` | Undo the decision |

Other commands: `alora-evv ledger` (recent visits), `alora-evv unmark <visit>` (undo a
"submitted" mark), `alora-evv sweep --from-csv export.csv` (work from a saved export).

## Web version

The same engine also runs as a website (`alora_evv/web`): sign in with an
alorasupports.com Google account, click Run sweep, review each visit, and open the state
form prefilled. `docs/WEB_APP_PLAN.md` explains hosting on a Google Cloud VM and the
setup checklist; `deploy/` has the Docker files. To try it locally without sign-in, set
`web.auth: none` in `settings.yaml` and run:
```
pip install -e ".[web]"
uvicorn --factory alora_evv.web.app:create_app --reload
```

## Adding rules and codes

Most changes are edits to `config/rules.yaml`: new portal error codes, new error types
with their own critical error, reason code and justification, and new service codes. Items marked
TODO there still need confirming against the state's guidance. If the state changes its
form, update the field IDs in `config/state_form.yaml`.

## Privacy

- Client names and Medicaid IDs exist only in memory while forms are filled. The portal
  export is downloaded to a temporary folder and deleted after each run. No screenshots.
- The data folder (ledger, portal session, AxisCare reports) lives outside the code
  folder, with owner-only permissions on Mac.
- The saved portal session lets the next run skip logging in. Delete
  `portal_session*.json` in the data folder to force a fresh login.

## Roadmap

1. **Now:** port of Yitzi's sweep, cross-platform, AxisCare instead of the Google Sheet,
   plus the web version. Next: confirm form prefill with a live test, deploy to the VM.
2. **Billing inbox watcher:** read state responses in the billing Gmail, record each visit
   as Accepted / Rejected / Educational in the app's own ledger, and log educational
   errors per caregiver.
3. **AxisCare API** backend replacing the report exports (`axiscare.py` → `ApiBackend`).
4. **Chrome extension** to fill the state form, only if URL prefill turns out not to work.
5. **More rules:** remaining reason codes (100, 110, 130), overnight splits, units checks.

## Development

`pytest` runs the tests (fake data only, including a mock of the state form).
See `CLAUDE.md` for the rules any AI coding assistant must follow in this repo.
