# Recovery rollout checkpoint — 20 October 2026

Personal activation is BLOCKED, not a passed recovery deployment. The user
confirmed that two private account bindings and dedicated test Calendars are not
available. No subjects, Calendar IDs, Moodle URLs, fake users or a shared Calendar
were invented. Existing serving revisions/settings were preserved.

## Source and rollback

Preparation started from `origin/main` `cb624cb1a4dc324af039ae075163e9d068d7f971`.
The old deleted GCP branch was not restored. Work used an isolated, detached
worktree; the original checkout's uncommitted files were preserved.

Build candidate: `ae7715cc81ffa6d8cf7473cc735a5b94b53d6ca0`.
Cloud Build: `2d2412d6-c9be-4333-a98c-9051f9e54214`, `us-central1`.
This candidate contains the integrated product source from `cb624cb` plus the
release verification infrastructure. Later helper/documentation fixes do not
change the product package's `src/` files or runtime dependencies.

| Existing component | Preserved revision / immutable image | Settings |
|---|---|---|
| Website | `campuspulse-00006-8xg`; `us-central1-docker.pkg.dev/gen-lang-client-0602946419/campuspulse/calendar@sha256:c98bd68e7b197938f1ba194c6f5a7172501d0fef769b460afa9e395e0455586e` | `campuspulse-calendar-settings:1`, Gemini key reference `campuspulse-calendar-gemini:1` |
| MCP | `campuspulse-recovery-mcp-00002-cmp`; `us-central1-docker.pkg.dev/gen-lang-client-0602946419/campuspulse/recovery-mcp@sha256:6e8f8f8d33b9bc166a3f1949d80620bc6bb1c0b81069788c81b3cee7a7cba636` | `campuspulse-recovery-settings:2` |
| Agent Engine | `projects/585736102295/locations/us-central1/reasoningEngines/3019439524657758208` | Existing configuration; zero personal account bindings |

Project: `gen-lang-client-0602946419` (`585736102295`), region `us-central1`,
Firestore Native database `campuspulse-calendar`. The public application keeps
Gemini API; recovery uses Vertex Gemini `gemini-3.5-flash-lite` in `global`.
Google client IDs and backend origins of both existing settings were compared
in memory and match; private values were not printed or committed.

Website provenance was checked against its successful Cloud Build
`1595e1d2-81f2-44fc-84d1-820e450cdb8b`: all 113 deployed `src/` files match
`cb624cb1a4dc324af039ae075163e9d068d7f971` after BOM/line-ending normalization.
That confirms the deployed website code, but its current settings have recovery
disabled. Existing MCP/Engine still contain the earlier bootstrap runtime; the
new per-user product package has not been installed on them in this checkpoint.

Full revision specs, traffic, immutable images, mounted secret versions, Engine
package references and resource IAM were saved in ignored
`.campuspulse-local/rollout-20261020/rollback.json`. The three existing Engine
staging artifacts were copied to new immutable backup object names in the private
Engine staging bucket; the mapping is in that private snapshot. This matters
because the SDK's default staging URIs are overwritten by future updates.

## Verification in this checkpoint

Cloud Build `2d2412d6-c9be-4333-a98c-9051f9e54214` completed with `SUCCESS` at
`2026-10-20T10:58:44.803730Z`. Its release gate ran in Linux/Python 3.12 with
Java 21, Firestore Emulator and PowerShell available:

| Check | Actual result | Boundary |
|---|---|---|
| Full pytest | 2073 tests; 0 failures, 0 errors, 0 skips | Firestore Emulator; synthetic external identity/model/Calendar edges |
| Recovery evaluation, `--mode offline` | 347 passed, 95 warnings | Scripted model and synthetic Google adapters |
| Voice evaluation, `--mode literal-loopback` | 53 passed | Protocol/loopback checks |
| `pip check` | No broken requirements found | Hash-pinned test dependencies; runtime stage separately passed pip check |
| Wheel and sdist | Both built | Same verified product source |
| MCP composition | `offline_mcp_composition_ok` | No tools dispatched; authentication unchanged |
| Native iOS | Not run on this Windows/Linux host | Requires a working Xcode/macOS environment |

The resulting image is
`us-central1-docker.pkg.dev/gen-lang-client-0602946419/campuspulse/recovery-rollout@sha256:c6c8163cdac74d296b7442b8b0f8aca38ca1d9fbd96a4f14fa79bfa40cd5c2b7`.
It is a verified candidate, not a serving revision. Artifact directory:
`gs://gen-lang-client-0602946419-campuspulse-build/recovery-rollout/ae7715cc81ffa6d8cf7473cc735a5b94b53d6ca0/`.
`qa-summary.json`, `pytest.xml`, evaluation logs, `voice-report.json`, wheel and
sdist were exported there. These are private build artifacts, not public URLs.

| Artifact | SHA-256 |
|---|---|
| `campuspulse_recovery-0.1.0-py3-none-any.whl` | `5fa13961584b5ed321e2b077f660802ab8ef264cfb8b3975ee2582739d39cf32` |
| `campuspulse_recovery-0.1.0.tar.gz` | `dc1e22f816baf1d38a6f6f2351ddd598f6f84d48d14ef14bea3c2341f5b9b9e9` |

Reproduce from a clean committed source tree with
`python deploy/recovery-rollout.py build`; inspect the returned build ID until
it is `SUCCESS`, then read the matching SHA's private `qa-summary.json` and image
digest. `deploy/run-rollout-checks.py` contains the exact check commands.

Existing cloud infrastructure was independently rechecked:

- Real Vertex generateContent as the agent identity returned one candidate.
- Firestore GET of a reserved nonexistent infrastructure document as the runtime
  identity returned 404 NOT_FOUND, confirming authorized read without user data.
- Anonymous MCP `/mcp` returned 403; agent-signed requests without grants reached
  the application and returned JSON `service_or_invocation_rejected` with 403.
- MCP `/healthz` returned frontend HTML 404 and was recorded separately.
- Backend identity read the exact Engine and actual operation schema, then invoked
  `async_query` through the real platform API. Invalid synthetic input returned
  HTTP 200 / `{status_code:403,payload:{error:service_or_invocation_rejected}}`.
- A valid-shaped synthetic invocation and invalid service ID token reached the
  actual ASGI worker's service-identity rejection boundary. No grants, user
  sessions, Calendar writes or Moodle writes were created by these checks.
- Temporary deployer impersonation bindings were removed after verification.

Real resource IAM remains scoped: backend
`campuspulse-calendar@gen-lang-client-0602946419.iam.gserviceaccount.com` has the
two-permission custom role on the exact Engine. MCP Cloud Run invoker is only
`campuspulse-recovery-agent@gen-lang-client-0602946419.iam.gserviceaccount.com`.
MCP runs as `campuspulse-recovery-mcp@gen-lang-client-0602946419.iam.gserviceaccount.com`.
Platform OAuth and the application service ID token were not merged or bypassed.

MCP origin and its ID-token audience are both
`https://campuspulse-recovery-mcp-585736102295.us-central1.run.app`.
The application agent ID-token audience is
`https://campuspulse-recovery-agent-585736102295.us-central1.run.app`.
The actual Engine operation schema remains:

```json
{"name":"async_query","api_mode":"async","parameters":{"type":"object","properties":{"invocation":{"type":"string"},"service_token":{"type":"string"}},"required":["invocation","service_token"]}}
```

The response is `{status_code, payload}`. `invocation` is a server-issued,
live-session-bound grant. It is not a user prompt. No `async_stream_query` adapter
was substituted, and no new Engine update operation is claimed in this checkpoint.
Historical completed deployment operations are recorded in
[RECOVERY-GCP-HANDOFF.md](RECOVERY-GCP-HANDOFF.md).

The build identity received `storage.objectCreator` only for objects under
`recovery-rollout/` in the existing private build bucket, to export QA artifacts.
It does not grant overwrite/delete or access to user Calendar/Moodle data.
An initial high-CPU build request was rejected by the region's quota before a
build ran; the candidate uses the existing standard machine quota. Billing and
quotas were not changed.

## Published UI observations

The exact public website was opened in the browser through its normal login
route. A normal authenticated session loaded ten Moodle task headings and the
saved chat. After browser reload, the session and task count persisted, with zero
captured console errors. No task titles, descriptions, account identifiers or
feed URLs were copied into this report.

Recovery/trace controls remain disabled for this unbound application. These are
observations of the existing website, not a new two-account login, newly generated
Gemini reply, planning save, tool dispatch, approval, Calendar apply or verify.
MyDU interactive provider login and two-account isolation were not exercised.

The existing `campuspulse-check` job was executed once as
`campuspulse-check-4djvv`; its immutable image was first checked to equal the
serving website's image. It completed successfully at
`2026-10-20T11:04:52.718023Z` with one successful task. Only this safe aggregate
was retained: `{"status":"ok","accounts":3,"tasks":30,"gemini":"answered"}`.
The job refreshed actual configured Moodle feeds and obtained a new Gemini reply
to a synthetic prompt. It may refresh the ordinary server-side feed cache, but
performs no Calendar/Moodle writes. This proves real Moodle/Gemini server edges
for the existing app, separately from the browser and recovery acceptance.

Current request path: browser -> Cloud Run `campuspulse` ->
`CalendarConversation` / `AdkConversationAgent` -> Gemini API; the optional
recovery backend is absent from current settings. The integrated source already
implements browser -> `CloudBackend` -> `AgentEngineTransport` ->
`client.agent_engines.get(...)` / `remote.async_query(...)` ->
`AgentEngineRuntime` -> Vertex agent -> private MCP. Activation requires matching
settings and the existing per-user code on Engine/MCP; no rewrite is needed.
See [calendar_app.py](../src/campuspulse_recovery/cloud/calendar_app.py#L52),
[recovery.py](../src/campuspulse_recovery/cloud/recovery.py#L25),
[engine_transport.py](../src/campuspulse_recovery/cloud/engine_transport.py#L13),
[runtime.py](../src/campuspulse_recovery/agent_engine/runtime.py#L72) and
[accounts.py](../src/campuspulse_recovery/cloud/accounts.py).

User bindings remain server-owned `calendar_feed_accounts` entries keyed by
`https://accounts.google.com#IMMUTABLE_GOOGLE_SUB`, with the verified Moodle user
and dedicated Calendar. Sessions, grants, plans, approval and trace use the same
Firestore database; users must sign in again after binding changes. Safe settings
names are `CAMPUSPULSE_CLOUD_SETTINGS`, `CAMPUSPULSE_CALENDAR_SETTINGS`,
`calendar_feed_accounts`, `recovery.settings` and `recovery.target`.
Existing Secret Manager references are
`projects/gen-lang-client-0602946419/secrets/campuspulse-recovery-settings/versions/2`,
`projects/gen-lang-client-0602946419/secrets/campuspulse-calendar-settings/versions/1`
and `projects/gen-lang-client-0602946419/secrets/campuspulse-calendar-gemini/versions/1`.
No new personal settings secret versions were created.

## Completing activation once private prerequisites exist

1. Obtain two allowed users' immutable Google subjects through ordinary verified
   sign-in, their Moodle user IDs and separate owner-designated test Calendar IDs.
   Export each feed as All events + Recent and next 60 days. Keep URLs and all
   private bindings outside Git. Share each dedicated Calendar with the MCP
   identity and verify writer access separately; Google login alone is insufficient.
2. Prepare matching local settings with:

   ```sh
   python deploy/recovery-rollout.py prepare-settings --bindings PRIVATE_BINDINGS --engine-settings PRIVATE_ENGINE_SETTINGS --calendar-settings PRIVATE_CALENDAR_SETTINGS
   ```

   Missing/empty bindings are checked before any cloud mutation. The helper
   requires two numeric immutable subjects and distinct explicit dedicated
   Calendar IDs; typed CloudSettings additionally enforces uniqueness and common
   project/database/origin/client ID. It does not claim to verify Calendar ACLs.
3. Use the candidate's SUCCESS image digest and exported wheel, not independent
   builds from changing working copies. On Windows, prepare the Agent Engine
   bundle using the exported `git archive` source directory so the strict wheel
   source-byte validation sees the same LF bytes as the Linux-built wheel.
4. Add new explicitly pinned Secret Manager versions for recovery settings and
   calendar settings. Preserve the existing versions for rollback. Do not replace
   `latest`, read the Gemini key into Git, or route real users to bootstrap.
5. Update existing MCP with `deploy/recovery-gcp.py deploy-mcp --image DIGEST
   --settings-version NEW_VERSION`; update the SAME Engine with `update-engine
   --bundle PRIVATE_BUNDLE --resource ACTUAL_ENGINE_ID` and wait for its completed
   operation. Read the remote schema and run authorized non-writing checks.
6. Only then deploy the website using a private copy of its deployment JSON with
   this build ID/tag, new settings version and unchanged Gemini secret reference.
   `deploy/deploy-calendar.ps1` now explicitly selects `cloud.calendar_app`, so the
   same unified image can safely serve MCP and website roles. The job already
   selects `cloud.calendar_check` explicitly. Preserve Scheduler identity/origin.
7. Sign the bound users in again after binding changes. Verify `/planning`
   persistence, real model-initiated MCP events, exact plan version/hash consent,
   approved apply only to each dedicated Calendar, independent re-read verify,
   trace/UI/Calendar agreement and replay without duplicates. Test stale consent,
   cross-account run denial and an unbound user's ordinary chat separately.

No component has been updated or activated from the candidate in this checkpoint:
doing so against absent real bindings would not satisfy the requested rollout.
The remaining blockers are private identities, two dedicated authorized Calendars,
their verified feeds and the consequent two-user interactive acceptance. They are
not evidence of absent GCP resources or missing backend query permissions.

Final helper-only checks passed: Python syntax for all three deployment/check
scripts, PowerShell parser for `deploy-calendar.ps1`, missing/empty bindings
rejected before cloud access, and `git diff --check`. The 11 pre-existing dirty
tracked files in the original checkout were checked against private SHA-256
snapshots and remained byte-identical. The full Linux gate was not rerun for
documentation/helper-only edits; product source and runtime locks are unchanged.

Changed rollout files: `deploy/Dockerfile.rollout`,
`deploy/run-rollout-checks.py`, `deploy/recovery-rollout.py`,
`deploy/recovery-gcp.py`, `deploy/deploy-calendar.ps1` and the safe checkpoint
links/reports under `setup/`. Product source, locks, user data and credentials
were not changed by this preparation. Commits `ea7dbb5` and `ae7715c` were pushed
to `origin/main` without force before the candidate build. The final report/helper
commit and independently read remote SHA are recorded in the chat delivery.
