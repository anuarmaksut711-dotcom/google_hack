# Google Calendar on the published website

The cloud Calendar connection reads events and can create new events from an
exact approved chat proposal. The local connection retains read-only scopes.
A signed-in user explicitly consents to their primary Google Calendar.
Google sign-in and Calendar consent are separate;
local credentials are not migrated into the cloud.

The website runs in `project-e25873c6-e889-40bb-86d`, `us-central1`, at
`https://campuspulse-403025736340.us-central1.run.app`.
Its existing Vertex AI conversation receives a fresh, curated Calendar projection
when the user sends a message. This change does not update or claim acceptance
of the separate Agent Engine recovery deployment.

OAuth uses the existing CampusPulse Web client and the exact callback
`/auth/google-calendar/callback`. The callback removes its query immediately and
completes through a same-origin POST. State, PKCE, nonce, signed Google subject,
the original sign-in challenge and the current session are checked server-side.

`CAMPUSPULSE_CALENDAR_OAUTH` points to a Secret Manager-mounted JSON document
containing the explicit project/database/origins, downloaded Web client, a Fernet
key and the allowed backend identity. Tokens and pending OAuth state are
encrypted before writing to Firestore. The ciphertext includes its document path.
The configured database is `campuspulse-calendar`; container disk is not durable
user storage. Disconnect replaces the saved binding and advances its generation.

The website calls the existing private MCP service at `/calendar/mcp` using its
platform ID token. The application additionally verifies that exact website
service identity. This transport exposes `read_calendar`, `apply_plan` and
`verify_plan`; each accepts
a short-lived single-use opaque grant, never a model-selected user or Calendar ID.
The service checks the live, non-revoked website session before and after reading.
The existing recovery MCP route retains its own agent identity and authorization.
Anonymous Cloud Run access to MCP must remain disabled.

Cloud consent adds `calendar.events.owned`. Google describes this scope as
create/update/delete; this integration implements create/get only. Calendar
identity is the separately consented signed Google subject, not necessarily the
website login subject. The prelogin flow still requires subject equality before
issuing its website session. A connection generation change invalidates plans.

The OAuth response parser accepts a Google-granted superset only after checking
that every requested scope is present; it never retries a one-use authorization
code or enables process-wide relaxed scope validation. A repeated connection to
the same signed subject and primary calendar preserves `binding_generation` for
existing plans. Credential `generation` still advances to prevent late reads
from overwriting rotated tokens. Disconnect or a changed identity invalidates
the binding. Callback errors distinguish an expired flow from a saved connection
and keep a link back to the site; flow selection is scoped to the browser tab.

Validated chat blocks produce an encrypted immutable proposal and source hash.
The backend records approval of that exact payload through the authenticated UI.
The model cannot approve. Durable state serializes application per calendar,
registers ownership before dispatch, and preserves each confirmed operation.
Deterministic create IDs and read reconciliation prevent duplicate inserts after
unknown outcomes. New availability invalidates stale approval; altered existing
events are never overwritten. Only actual final reads produce verified success.

Cloud Calendar reads cover the current local day, including elapsed events,
and the next 72 hours with bounded pagination and expanded
recurrences. All-day end dates are exclusive. Malformed or partial reads fail;
they do not become an empty calendar or a successful synchronization. Event
titles are untrusted data. An event in the past is not proof of completion.

Build preparation: `deploy/prepare-connected-calendar.py` takes explicit project,
region, account, local state directory and downloaded OAuth client file. Secret
material stays outside the uploaded `source/` directory. The overlay is based on
the currently serving immutable images and fails on source conflicts. Save the
previous revisions, image digests, traffic and Secret Manager references before
deploying. Keep deployed branding, voice and workspace changes when reconciling
an overlay conflict. Run `pip check` in both candidate containers before rollout.
The initial key generation requires `--new-oauth`; subsequent deployments must
reuse the pinned Secret Manager version. Never regenerate the key for existing
Firestore ciphertext. Temporary local configuration is protected with Windows
DPAPI after the Secret Manager upload.

Calendar facts passed to chat contain a server-computed local date and per-event
`day_label`, including the local-midnight boundary. The model must use these
labels rather than infer today's date from the first upcoming event.

Verification must include real OAuth consent, authenticated MCP Calendar read,
the actual day plan in the website, an actual Vertex chat answer and anonymous
MCP rejection. Unit tests alone do not prove Google connectivity or deployment.

On 2026-10-21 the current website profile completed real write consent. A chat
request proposed “Встреча по проекту” for 2026-10-22 10:00–10:30 UTC+5.
The user approved that exact version, the UI applied it through private MCP,
and actual Calendar reads verified it. Its Calendar event dialog was opened
independently. The four existing events for October 21 remained unchanged.
