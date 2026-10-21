# Read-only study agent rollout — 22 October 2026

This release connects the ordinary website conversation to an existing managed
Agent Engine. It does not activate recovery or Calendar writes. Cloud acceptance
is pending until the final verification section records an actual website run.

## Current topology

- Repository: `anuarmaksut711-dotcom/google_hack`; integration branch:
  `codex/pitch-agent-cloud`.
- Project: `project-e25873c6-e889-40bb-86d` (`403025736340`), `us-central1`.
- Website: `https://campuspulse-403025736340.us-central1.run.app`.
- Existing Engine: `projects/403025736340/locations/us-central1/reasoningEngines/8841397574196789248`.
- Engine identity: `campuspulse-deadline-agent@project-e25873c6-e889-40bb-86d.iam.gserviceaccount.com`.
- Website identity: `campuspulse-calendar@project-e25873c6-e889-40bb-86d.iam.gserviceaccount.com`.
- Private MCP: `https://campuspulse-mcp-65xrcfoq2q-uc.a.run.app/study/mcp`.
- Durable state: Firestore database `campuspulse-calendar`.
- Model: existing Vertex Gemini target from website settings, currently
  `gemini-3.5-flash-lite` in `global`.

The existing recovery Engine and its own service identity remain separate.
The website's legacy `application.identities` contain historical bootstrap roles;
the study deployment uses explicit identities in `study_agent` instead.

## Request and tool contracts

`POST /api/chat` keeps its existing request and idempotency contract. The backend
creates a short-lived invocation capability bound to the authenticated owner and
live session. It calls the existing managed custom operation:

```text
async_query(invocation, service_token) -> {status_code, payload}
```

`invocation` is an opaque server grant, not a prompt. The Engine checks the exact
website identity before consuming it. Gemini ADK chooses between these functions:

- `read_assignments()`: calls private MCP to refresh/read the signed-in user's
  real Moodle iCalendar feed; excludes locally completed tasks and returns bounded
  titles, courses, deadlines and source freshness.
- `read_schedule()`: calls private MCP to read that user's saved weekly classes
  and coverage. Missing classes do not prove personal availability.

Each MCP call uses a new single-use capability bound to its parent invocation,
owner, live session and exact tool. The model receives no account selector, feed
URL, OAuth token or grant. Expiry and revocation are checked after source I/O too.
The study identity is rejected by the legacy Calendar/recovery transports.

The managed result contains `reply` (existing `AdviceReply` JSON), `trace`,
`class_schedule`, and `run_id`. The website independently rereads the current
class schedule and checks the plan before saving the visible answer. It does not
eagerly read Google Calendar or prepare a Calendar write proposal in this mode.

The saved turn/API optionally exposes `tool_trace` and `agent_run_id`.
Each trace entry contains only `tool`, `status`, `duration_ms`, and `result_count`.
Frontend visualization can consume those fields in the next UI stage; existing
clients remain compatible. Trace is actual runtime data, not model-written text.

Timeouts: Engine 105 seconds, transport 120, website request 135, active-turn
lease 150. Reusing a request key never redispatches the Engine. Failure does not
silently fall back to direct Gemini.

## Reproducible release

1. Commit the integrated source and export that exact SHA with `git archive`.
   Use the LF export for both the wheel and Engine deployment validation.
2. Submit `deploy/study-cloudbuild.json` with `_SOURCE_SHA` set to that SHA.
   `deploy/Dockerfile.rollout` runs the existing complete Linux release gate,
   including Firestore Emulator, evaluations, package build, and `pip check`.
3. Use its single immutable runtime image for the website and MCP. Retrieve the
   wheel from the same build's private verification artifacts.
4. Save current Engine package references, service revisions, traffic, settings
   versions and IAM before changing the existing resources. Use unique staging
   object names so the previous Engine package remains recoverable.
5. Add an optional `study_agent` object to the existing website settings secret:
   `target`, `agent_audience`, `mcp_origin`, `backend_service_account`, and
   `agent_service_account`. Keep `recovery` unset. Private settings stay outside Git.
6. Mount that settings version in MCP, select
   `campuspulse_recovery.cloud.study_mcp:create_app`, and permit only the existing
   deadline-agent identity to invoke the private service in addition to its
   existing authorized callers. Preserve existing Calendar OAuth mounts.
7. Run `deploy/study-agent.py --help` for the explicit update-only command. Use
   the existing Engine resource above, the exact verified wheel and hash-pinned
   requirements, and the existing private staging bucket. No duplicate Engine
   is created. Preserve the Engine's deadline-agent service account.
8. Activate the website's new image/settings only after MCP and Engine are ready.
   Verify through the actual signed-in website and inspect the saved turn trace.

Cloud Build can create artifacts only under the `study-agent-20261022/` prefix
of the existing private build bucket. Source artifacts and settings are not
published as public URLs.

## Verification boundaries

Unit/integration tests exercise the actual ADK tool loop and MCP protocol with
synthetic model/external I/O, session isolation, grant expiry/replay, source
freshness, post-generation schedule checks and absence of Calendar write calls.
Those checks do not by themselves establish live Google/Moodle connectivity.

Before this release, some already-serving Calendar/backend modules were missing
from GitHub. Their inspected serving versions were restored to keep existing
features reproducible. The new study path cannot invoke their write tools.
No frontend assets or view layout changes are part of this integration.

Final source SHA, immutable image, revisions and live verification results will
be recorded after the real rollout. A failed or unavailable source must remain
an explicit failure, not an empty successful list or a claimed completed plan.
