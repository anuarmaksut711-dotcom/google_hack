# Moodle calendar + Gemini in Google Cloud

This is the deployed version of the local calendar application, not the separate
Sheets/Calendar recovery agent or Agent Engine runtime.

- Project: `gen-lang-client-0602946419`, region: `us-central1`.
- Application: https://campuspulse-585736102295.us-central1.run.app/login
- Entry point: `python -m campuspulse_recovery.cloud.calendar_app`.
- Database: Firestore Native, `campuspulse-calendar`.
- Runtime identity: `campuspulse-calendar@gen-lang-client-0602946419.iam.gserviceaccount.com`.
- Scheduled refresh: `campuspulse-calendar-sync`, every 15 minutes, Asia/Qyzylorda.

Google sign-in verifies the signed token, audience and one-use nonce. Each verified
Google subject receives its own Moodle connection, cache, completion marks and
chat history. Anonymous requests cannot read personal data. Scheduled refresh
requires a signed service token from the configured scheduler account.

Feed URLs stay on the server. Gemini receives the existing minimal source
projection: subjects, task names and deadlines, with completed items excluded.
The iCalendar feed does not provide grades or submission status; this deployment
does not add those capabilities or write plans into Google Calendar.

All durable state is in Firestore. Container disk is not used for user data.
Refresh attempts use a transactional one-minute cooldown shared by replicas.
Failed refreshes preserve the last successful snapshot. Completion marks are
updated in a separate transaction and survive refreshes and restarts.

The existing CampusPulse Gemini API key is mounted from a pinned Secret Manager
version. The build uses a source allowlist; local configuration, caches, TLS keys,
credentials, copied repositories and virtual environments are not uploaded.
Cloud Run has zero minimum and two maximum instances, so an idle service may
have a cold start. The laptop is not part of the serving or scheduled-sync path.

## Deploy and update

Run from the repository root in PowerShell 7 with the authorized Google account:

```powershell
# Initial infrastructure setup; does not attach billing.
./deploy/prepare-calendar.ps1 -ProjectId gen-lang-client-0602946419 -ProjectNumber 585736102295

# Build only allowlisted source assets. This command returns a build ID.
./deploy/build-calendar.ps1

# Once that Cloud Build has status SUCCESS, deploy its immutable image digest.
./deploy/deploy-calendar.ps1
```

Deployment metadata is saved in ignored `.campuspulse-local/calendar-deployment.json`.
The OAuth Web client must allow the exact application origin. This has been added
to the existing CampusPulse Web client without removing the localhost origin.

To migrate the current CLI user's local calendar once, use the development Python
environment with `src` on PYTHONPATH and run `deploy/migrate-calendar.py`. It verifies
the CLI identity token, binds the calendar to that Google subject, preserves
existing cloud marks and refuses to replace a different cloud connection. It does
not migrate local browser sessions or the old single-user in-memory conversation.

## Verification

```powershell
Invoke-RestMethod https://campuspulse-585736102295.us-central1.run.app/health
gcloud run jobs execute campuspulse-check --region=us-central1 --project=gen-lang-client-0602946419 --wait
gcloud scheduler jobs run campuspulse-calendar-sync --location=us-central1 --project=gen-lang-client-0602946419
```

The private smoke job uses the deployed image, runtime identity, Firestore and
mounted key. It checks Moodle and makes one real Gemini request without personal
source data. It logs only success, account/task counts and a safe provider status.
There is no public test endpoint or alternate authentication route.

Automated tests: `tests/lms/test_cloud_calendar.py`, the existing multiuser/calendar
feed tests and the web chat tests. Tests cover persistence, account isolation,
cooldown across replicas, failed refresh preservation, connection changes across
replicas and rejection of anonymous requests. Mock-based tests do not substitute
for the deployed job, Scheduler dispatch, or a real browser sign-in check.

## Verified deployment — 20 October 2026

- Serving revision: `campuspulse-00002-h47`, 100% of traffic.
- Cloud Build: `72f85abf-9620-46fd-920a-de2b95f0553f`, SUCCESS.
- `/health`: HTTP 200 with Firestore storage configured.
- Anonymous personal-data and scheduled-sync requests: HTTP 403.
- Scheduler dispatch: HTTP 200, 20 October 2026 at 14:01:59 Asia/Qyzylorda.
- Cloud smoke execution `campuspulse-check-8wgc4`: succeeded; real Moodle and Gemini.
- Migrated 10 tasks and 3 completion marks to the verified CLI user's Google account.
- The user confirmed Google sign-in, visible tasks and a Gemini reply in the cloud UI.
- 28 scoped automated checks passed; the final health-route and replica-binding
  changes were also checked with the three cloud-calendar tests.

These checks confirm the deployed calendar/chat path. They do not claim Moodle
grade access, Google Calendar writes or a deployed Agent Engine.
