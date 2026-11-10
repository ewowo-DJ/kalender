# My Calendar

A shared calendar for Ewoud and Helen, with events (time and location), tasks, and HOGENT classes pulled in from TimeEdit every day.

- **App:** `index.html`, installable on Android (Chrome → ⋮ → Install app). Opens full screen with no browser bar.
- **Sign-in and sync:** Firebase (Google sign-in plus a Firestore database). Only the two addresses in `firestore.rules` get access.
- **TimeEdit + Google Agenda:** `.github/workflows/timeedit.yml` runs `scripts/sync_timeedit.py` every morning. Links are pasted in the app under the gear menu → Koppelingen.

## Settings

| Where | What |
|---|---|
| `config.js` | Firebase web settings (not secret) |
| `firestore.rules` | Paste into Firebase → Firestore Database → Rules |
| GitHub → Settings → Secrets and variables → Actions | `TIMEEDIT_URL` (Ewoud's .ics link), `TIMEEDIT_URL_HELEN` (Helen's .ics link) and `FIREBASE_SERVICE_ACCOUNT` (the full contents of the service-account key file) |
| Firebase → Authentication → Settings → Authorized domains | Add `<username>.github.io` |
