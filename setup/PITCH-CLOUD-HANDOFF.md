# Read-only study agent rollout — 22 October 2026

This release connects the ordinary website conversation to an existing managed
Agent Engine. It does not activate recovery or Calendar writes. The main website,
private MCP and existing Engine are deployed; the signed-in live verification
below confirms actual tool calls and a plan using the user's Moodle source.

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

A failed or unavailable source must remain an explicit failure, not an empty
successful list or a claimed completed plan.

## Completed rollout and live acceptance

- Runtime source: `1382f49aaba7c37c6571bbd8355349064e039012`, pushed to `main` and
  `codex/pitch-agent-cloud`, authored and committed by `bossmerej-ship-it`.
- Cloud Build: `6cf85845-7ce5-4fb7-9518-47e84adbbcf0`, `SUCCESS`.
  The full Linux gate passed: 2308 tests, zero failures, errors or skips;
  dependency check, recovery/voice evaluations, real MCP ASGI composition and
  package build also passed.
- Website revision: `campuspulse-study-1382f49`, main traffic 100%.
- Private MCP revision: `campuspulse-mcp-study-1382f49`, main traffic 100%.
- Both use the immutable image
  `us-central1-docker.pkg.dev/project-e25873c6-e889-40bb-86d/campuspulse/study-agent@sha256:2fba38a05045c82073c424eac267808af62684ac97ab96d47c4142b724ce2764`.
- Settings secret version: `3`. OAuth secret version remains `1`.
- Engine update completed with operation `async_query`; verified wheel SHA-256:
  `8c625bea90d8db27f4f7fe4a1a88c483341b6a89c508668640a333ddfe30014e`.
- Public `/health` reports Firestore, `gemini-3.5-flash-lite`, and
  `agent_runtime: agent_engine`. Invalid Engine service/invocation input returns
  the expected rejection without source reads.

On 22 October 2026, a normal Google sign-in on the main website was used for
two real chat requests. No synthetic user, copied feed URL, mocked tool result
or direct-Gemini fallback was used for this acceptance:

| Request | Actual persisted tool trace | Website result |
| --- | --- | --- |
| Closest uncompleted Moodle deadlines | `read_assignments`: completed, 15 records, 3224 ms | Answer with actual source deadlines; end-to-end 39336 ms, including initial source/worker warm-up |
| Plan for 22–24 October, 19:00–21:00 Asia/Qyzylorda, 45 minutes per task | `read_assignments`: completed, 15 records, 3687 ms; `read_schedule`: not_connected, 0 classes, 892 ms | Three 45-minute study blocks at 19:00 on the requested dates; end-to-end 7779 ms |

The plan was correctly presented as a **draft**: the user's weekly class schedule
is not filled in, so conflicts with classes could not be verified. The user's
explicit availability supplied the study windows. Moodle calendar feeds do not
establish submission/completion status; local completion flags are filtered and
the unknown source submission status is retained. The plan is a chat
recommendation; no Google Calendar event, proposal or write was created.

Both turns have nonempty `agent_run_id` values and actual `tool_trace` entries
saved in chat history. The browser reported no console errors. Private local
verification artifacts contain the sanitized trace summary and a screenshot;
personal conversation/feed contents are not committed to the repository.

## Real-time MCP trace interface

The subsequent UI integration adds a native, keyboard-accessible "Действия
агента" disclosure to each chat reply. It shows actual tool names, source
outcomes, result counts and measured duration, and preserves missing schedule,
unavailable source and valid empty-source distinctions. Existing saved
`tool_trace` records also render without rerunning tools. The completed trace
takes precedence over a partial live observation snapshot.

During `POST /api/chat`, the browser polls the existing authenticated
`GET /api/chat?chat_id=...`; GET never dispatches the Engine or writes Calendar.
No model thoughts, prompts, grants, source URLs or account selectors are exposed.
The optional turn field is:

```text
agent_progress: {
  phase: queued | thinking | tools | composing | completed | failed,
  tool_calls: [{call_id, tool, status, duration_ms?, result_count?}]
}
```

Each call is `read_assignments` or `read_schedule`; its status is `running`,
`completed`, `failed`, `unavailable`, `not_connected` or `unknown`. Counts and
durations appear only when reported, and missing observations do not imply a
tool call or success. The worker reports actual tool boundaries and ADK model
dispatch callbacks. Live progress is separately bound to owner, session, chat,
request and the one-use parent invocation; bounded snapshots persist with the
settled chat turn. Auth/revocation checks still fail closed. An observation-only
storage failure does not abort an otherwise valid tool/model operation.

Polling stops on a settled result, session/chat change or bounded timeout. A
failed poll preserves the displayed observations and never repeats a POST.
The UI retains native disclosure state, respects reduced motion and continues
the existing smooth reveal of the final response.

Deployment of this integration requires the same new artifact in MCP, Engine
and website: the invocation record now has optional `progress_id`. Update MCP
first, Engine second, and activate the website last. Older invocations omit the
field and remain supported. Existing settings version 3, service identities,
private MCP access and Calendar write restrictions are unchanged. Live rollout
results for this follow-up are recorded after verification below.

When later integrating `codex/pitch-ui-demo` (inspected at `15bb5fd`), keep a
single chat activity renderer. That branch already has a saved-only
`appendChatToolTrace` helper; this release's `CampusPulseStudyTrace` handles both
saved observations and the live lifecycle. Rendering both would duplicate the
same calls. The current release intentionally integrates only chat activity,
without merging unrelated planning, Calendar or presentation changes from that
branch. The user's explicit request authorizes this scoped chat UI update.

The website calls the same Engine `async_query` operation through the fixed
regional authenticated REST endpoint. Platform ADC access tokens authorize the
outer API call; the existing service ID token remains inside the private
operation input. No service-account key, discovery request, redirect or retry is
introduced. ADC discovery/refresh runs off the web event loop, and both time and
streamed response size are bounded. This avoids the synchronous package scan
observed in the Vertex SDK's lazy Agent Engines client on a cold web instance.

Pre-progress `chat-v1` records retain their exact canonical serialization and
digest: absent progress remains absent. The brief first trace rollout's explicit
null shape has a separate exact decoder; original bytes and hashes are checked
before conversion. Non-null progress remains durable for reload. Consequently,
the older `1382f49` application can read ordinary new turns but cannot read turns
with non-null progress; use a progress-compatible image when rolling back after
this feature is accepted. Existing records must never be deleted or rehashed to
work around a decoding failure.

Managed chat titles use the first seven words of the same user's message,
bounded to 60 characters and validated by the existing workspace rules. Naming
does not start a second local model after the Engine finishes. Manual names
remain protected, and ordinary non-managed chat and speech behavior are
unchanged. This website-only adjustment does not change the MCP or Engine
operation contract.

## Verified trace release artifacts

| Component | Runtime source | Verification / deployment |
| --- | --- | --- |
| Private MCP and existing Engine | `e0e866ec7aaa70ac0de22e0854d7ef7ffe8fdb3b` | Cloud Build `fcc15acc-ef60-4e99-a7bd-335da81ecc16`, 2354 tests, no failures/errors/skips; dependency, evaluation, package and MCP composition gates passed |
| Website, including the title latency fix | `4ffacf70192d35657cba2d8f041214570219adec` | Cloud Build `7f5c7321-b432-483e-99c6-f9076c169311`, 2360 tests, no failures/errors/skips; all release gates passed; GitHub CI `39680021992` passed |

The MCP revision is `campuspulse-mcp-trace-e0e866e`. Its image is
`us-central1-docker.pkg.dev/project-e25873c6-e889-40bb-86d/campuspulse/study-agent@sha256:87290ceba6df2a518d4e2082f1f1ada4e43d1af4ec40e3fc7f3bca47e16422ca`.
The same verified package updated the existing Engine, with wheel SHA-256
`3d883f13e5e9044c4b2c4054d98327273f9778acd8c3664eb748e84dfcd9a6e4`.
The completed Engine update timestamp is `2026-10-21T21:28:45.405569Z`.

The website revision is `campuspulse-trace-4ffacf7`, with image
`us-central1-docker.pkg.dev/project-e25873c6-e889-40bb-86d/campuspulse/study-agent@sha256:eb4a47ce6c99e1abf94be017a9540289036fc0ab14bd01a55c3c9560b56989c7`.
Its package SHA-256 is
`6fee53da7169e81a1d27f75da016dcb57db490ae158fe7cd0da70f8b40c3458b`;
the website-only title adjustment did not redeploy the compatible MCP/Engine.
Both Cloud Run services use settings secret 3, OAuth secret 1 and the existing
service accounts. The Engine still exposes only `async_query`.

The application changes are authored and committed by `bossmerej-ship-it` and
pushed to both `main` and `codex/pitch-agent-cloud` in
`anuarmaksut711-dotcom/google_hack`. No history rewrite is used for this release.
For a website-only rollback after trace adoption, use
`campuspulse-trace-e0e866e`, which can read the new saved progress records.

## Signed-in browser acceptance of the trace release

The normal Google session on the main website exercised the real managed agent
and private MCP. A running `read_assignments` call was visible before completion,
then the actual source outcomes appeared while the model prepared its answer.
Collapsing the native disclosure with the keyboard survived live updates;
reopening it and reloading the page retained the saved observations without
another Engine invocation. The existing smooth answer reveal remains in place.
The component was inspected in light and dark themes; the desktop browser had
one activity card and no horizontal overflow or console errors.

The final website revision was tested with its first new conversation after the
Engine had idled. It returned 15 actual Moodle records and the explicit
`read_schedule: not_connected` outcome. The answer contained three 45-minute
study blocks for 22–24 October, starting at 19:00 Asia/Qyzylorda. It was correctly
labelled a draft because the weekly class schedule is not filled in. No Calendar
event, saved Calendar proposal or write was created.

This cold request took 65.758 seconds in the managed path and 66.063 seconds for
the full POST. All 45 observed progress GETs returned 200: median 0.168 seconds,
maximum 0.483 seconds. Thus cloud startup/source latency remains visible, while
the website stays responsive and does not add the previous local title-model
pause after the reply is saved. The short title was generated automatically
without another model, and the same completed run and trace survived reload.
An earlier warmed-Engine request completed its managed path in 13.056 seconds;
these timings describe different startup conditions, not a performance promise.

Sanitized local evidence and browser screenshots are kept in ignored validation
folders. User messages, source URLs, tokens and personal source contents are not
published as repository artifacts. The full tests use synthetic external edges
or the Firestore emulator; the signed-in checks above establish live connectivity
only for the tested account and requests.
