# Google Calendar on the published website

The Calendar connection is read-only. A signed-in user explicitly consents to
their primary Google Calendar. Google sign-in and Calendar consent are separate;
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
service identity. The only tool on this transport is `read_calendar`; it accepts
a short-lived single-use opaque grant, never a model-selected user or Calendar ID.
The service checks the live, non-revoked website session before and after reading.
The existing recovery MCP route retains its own agent identity and authorization.
Anonymous Cloud Run access to MCP must remain disabled.

Calendar reads cover the next 72 hours with bounded pagination and expanded
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

Verification must include real OAuth consent, authenticated MCP Calendar read,
the actual day plan in the website, an actual Vertex chat answer and anonymous
MCP rejection. Unit tests alone do not prove Google connectivity or deployment.
