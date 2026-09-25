# Notes for Claude Code working on this repo

This app prepares Nebraska DHHS EVV adjustment requests for Alora Supports and
its sister agency First Choice. It handles PHI, so these rules are fixed:

- **Never add code that clicks Submit** (or Sign, Save as draft) on the state form, or
  that changes records in the Mobile Caregiver+ portal. The guard in `browser.py` stays.
- **No real visit data in the repo or in this conversation.** Tests use the fake data in
  `tests/fixtures/`. Real exports, AxisCare reports, the ledger and sessions live in the
  per-user data folder (`alora-evv where`), which is outside the repo. Don't read files
  from that folder.
- **No client names or Medicaid IDs written to disk** (reports, logs, ledger).
  They exist only in memory while a form is filled.
- **Secrets only through `credentials.py`** (Keychain / Credential Manager).
- Business rules belong in `config/rules.yaml`, not in code, wherever possible.
  Form field IDs belong in `config/state_form.yaml`.
- Run `pytest` and `pyflakes alora_evv tests` after every change. Must work on Mac and Windows:
  use `pathlib`, no shell-specific commands, no hard-coded paths.
- The web version (`alora_evv/web`) must keep Google sign-in on in production
  (`web.auth: google`); `none` is for local testing only. Pages that show visit data
  send `Cache-Control: no-store`.
