# Chat pages and saved Moodle connection: deployed 20 October 2026

- Site: https://campuspulse-403025736340.us-central1.run.app/
- Settings: https://campuspulse-403025736340.us-central1.run.app/settings
- Google Cloud project: `project-e25873c6-e889-40bb-86d`, region: `us-central1`.
- Cloud Run service: `campuspulse`.
- Serving revision: `campuspulse-pages-35e32c3`, 100% of traffic.
- Source commit: `35e32c3`, based on current workspace release `85ef24a`.
- Cloud Build: `34196df9-300d-4a5d-a2c1-53f2b25a7758`, SUCCESS.
- Image: `us-central1-docker.pkg.dev/project-e25873c6-e889-40bb-86d/campuspulse/calendar-pages@sha256:9a8e185ee96b867d40c27dab07233e5d104e8ebf4b48c5014d1429555ed4f2ea`.

This release adds per-user chat pages/folders and saved Moodle connection settings.
It retains the existing sign-in, chat behavior, dark workspace, model configuration,
Firestore database and runtime service account. Only the website image and traffic
were updated; no settings secrets, IAM grants or user connections were replaced.
Build source was restricted to code/assets and the required dependency/build files.

Verification completed:

- `tests/lms` and `tests/web`: 434 passed, one Firestore-emulator check skipped.
- Local browser with synthetic identity/data: create folder/chat, independent
  history, existing composition/scrolling, connect Moodle, return to settings.
- New revision health before traffic promotion: HTTP 200 and real Firestore read.
- Public origin after promotion: `/health` and `/login` HTTP 200.
- Anonymous chat, chat-workspace and Moodle API requests: HTTP 403.
- Anonymous `/settings`: redirects to `/login`.
- Cloud Run read-back confirms the immutable image and 100% revision traffic.

Authenticated production UI verification was not completed: browser automation
timed out at the Google account chooser. Local browser tests and authenticated
HTTP tests use synthetic identities; they do not prove a fresh production SSO flow.
This website release does not establish an Agent Engine deployment or claim that
any Google Calendar writes were performed.
