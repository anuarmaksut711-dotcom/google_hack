# Recovery GCP handoff

Latest rollout checkpoint (20 October 2026):
[RECOVERY-ROLLOUT-2026-10-20.md](RECOVERY-ROLLOUT-2026-10-20.md).
The integrated candidate passed 2073 tests with Firestore Emulator and was built
as an immutable image/wheel. The user confirmed that the two real private account
bindings and dedicated test Calendars do not exist yet. Existing website/MCP/Engine
serving versions were therefore preserved; no personal activation or Calendar
apply/verify success is claimed. The checkpoint supersedes component-version and
remaining-integration statements below; the earlier deployment receipts remain
historical evidence.

Status: Agent Engine deployment/update completed, private MCP Ready, remote
custom-operation smoke verified as the actual backend identity. Full per-user
recovery is NOT enabled: the application integration dependencies below remain.
Verified on 20 October 2026, Asia/Qyzylorda.
Project: `gen-lang-client-0602946419` (`585736102295`); region: `us-central1`.
The existing public `campuspulse` calendar/chat service must remain unchanged.

Repository integration update: source merge `9c2d16d` on `main` combines this
GCP work with the personal website recovery integration (`71bf31f`). The code
now implements per-user feed/source/Calendar resolution. The deployed bootstrap
runtime still requires a matching code/settings update and actual private account
bindings before site activation; see [RECOVERY-WEB-HANDOFF.md](RECOVERY-WEB-HANDOFF.md).
The cloud receipts below describe the earlier audited deployment, not a deployment
of the merged source.

## Backend contract

Deploy the existing `campuspulse_recovery.agent_engine.runtime.AgentEngineRuntime`.
Custom operation: `async_query(invocation: str, service_token: str) -> dict`.
Register operations: `{"async": ["async_query"]}`. Do not use `async_stream_query`.
Response: `{"status_code": 200|403|503, "payload": {...}}`.
A successful platform request does not establish a successful recovery result.
Never retry an ambiguous write automatically.

`invocation` is a server-issued Firestore grant (43 URL-safe characters), not
user/model arguments. Preserve live-session checks, exact plan approval, resource
bindings, grant claiming and independent verification. The backend supplies an
OAuth access token to Agent Engine AND a separate Google-signed backend service
ID token as `service_token`. Never expose either credential to browser/model/logs.

## Actual infrastructure parameters

- Runtime identity: `campuspulse-recovery-agent@gen-lang-client-0602946419.iam.gserviceaccount.com`.
- MCP identity: `campuspulse-recovery-mcp@gen-lang-client-0602946419.iam.gserviceaccount.com`.
- Backend identity: existing `campuspulse-calendar@gen-lang-client-0602946419.iam.gserviceaccount.com`.
- MCP origin/audience: `https://campuspulse-recovery-mcp-585736102295.us-central1.run.app`.
- Worker logical ID-token audience (`agent_origin`): `https://campuspulse-recovery-agent-585736102295.us-central1.run.app`.
  This is an audience, not a separate deployed worker; runtime hosts ASGI in-process.
- Backend origin: `https://campuspulse-585736102295.us-central1.run.app`.
- Firestore: `projects/gen-lang-client-0602946419/databases/campuspulse-calendar`.
- Staging bucket: `gs://gen-lang-client-0602946419-campuspulse-agent-engine`.
- Settings secret: `projects/gen-lang-client-0602946419/secrets/campuspulse-recovery-settings/versions/2`.
- Agent Engine resource ID: `projects/585736102295/locations/us-central1/reasoningEngines/3019439524657758208`.
- MCP Ready revision: `campuspulse-recovery-mcp-00002-cmp`, 100% traffic.
- MCP immutable image: `us-central1-docker.pkg.dev/gen-lang-client-0602946419/campuspulse/recovery-mcp@sha256:6e8f8f8d33b9bc166a3f1949d80620bc6bb1c0b81069788c81b3cee7a7cba636`.
- Cloud Build: `a844d48b-e282-4753-865c-194534ff8e7f`, SUCCESS; Linux tests: 61 passed.
- Vertex: provider `vertex`, model `gemini-3.5-flash-lite`, model location `global`.
  A synthetic generateContent request as the actual agent identity returned a candidate.
- Backend query role: `projects/gen-lang-client-0602946419/roles/campuspulseRecoveryCaller`;
  get/query permissions bound on this exact engine's resource IAM policy.
  There is no project-wide backend Vertex user/admin grant from this work.

Use named backend configuration such as `CAMPUSPULSE_AGENT_ENGINE_RESOURCE`,
`CAMPUSPULSE_AGENT_ENGINE_LOCATION`, `CAMPUSPULSE_AGENT_ID_TOKEN_AUDIENCE`,
`CAMPUSPULSE_CLOUD_SETTINGS`; the exact backend loader is owned by the integration developer.

## User binding dependency

The current runtime `CloudSettings.application.student` is a fixed, explicitly
bound owner; the current public site is multi-user. Cloud provisioning cannot
turn that into safe per-user recovery. The backend/runtime integration must resolve
Google subject -> live session -> owner -> explicit authorized Calendar/source
binding on the server. Do not give all users a shared Calendar or infer `primary`.
An infrastructure bootstrap configuration, if used, must be inert and never routed
to real users. No user Calendar will be used for deployment smoke.

Current Workspace adapter uses explicit resource sharing to the MCP service
account. Google sign-in alone does not authorize Calendar access. Per-user OAuth
or explicit per-user resource sharing remains a separate integration dependency.

## Cloud verification evidence

Create operation `6579780921266798592` and configuration update operation
`7409287677633101824` both completed without error on the same engine resource.
Remote schema contains exactly one async method, `async_query`, with two required
string parameters `invocation` and `service_token`. No `async_stream_query` adapter
was substituted. Firestore GET of a reserved nonexistent infrastructure document
as the runtime identity returned NOT_FOUND (authorized read, zero writes).

The first MCP revision failed because bootstrap used an eight-column assignments
range; the existing reader requires six. Secret version 2 corrects that range;
MCP revision 2 is Ready. The same corrected settings were applied to the existing
Agent Engine without creating a duplicate resource. Secret version 1 is disabled;
version 2 is pinned in the MCP revision. The Engine serializes the corresponding
validated configuration into its private staged artifact, without credentials.

Windows initially had missing test dependencies and a network-fence/socketpair
conflict. The authoritative Linux Cloud Build run completed all 61 tests. A new
offline composition check also constructs the actual production MCP factory
without dispatching tools or relaxing authorization.

The website was not deployed by this branch. Another concurrent deployment changed
its revision during this work; the observed `campuspulse-00005-2lj` still returns
healthy Firestore/Gemini configuration. Do not roll back concurrent website work.

Actual remote schema (read from the deployed resource):

```json
{"name":"async_query","api_mode":"async","parameters":{"type":"object","properties":{"invocation":{"type":"string"},"service_token":{"type":"string"}},"required":["invocation","service_token"]}}
```

Safe smoke results:

| Check | Actual result | Scope |
|---|---|---|
| Agent Engine GET as backend | Success; expected agent service account and schema | Real API/IAM |
| Engine testIamPermissions as backend | `aiplatform.reasoningEngines.get`, `aiplatform.reasoningEngines.query` | Resource IAM |
| `async_query` invalid-shaped synthetic input as backend | HTTP 200, output `{"status_code":403,"payload":{"error":"service_or_invocation_rejected"}}` | Real custom-operation dispatch; no recovery success claim |
| `async_query` 43-character synthetic invocation + invalid service token as backend | Same 403 envelope | Real ASGI worker startup and service-identity rejection; no grant/source/calendar dispatch |
| MCP `/mcp`, anonymous | HTTP 403 | Cloud Run IAM |
| MCP `/mcp`, agent-signed without grant | HTTP 403 JSON `service_or_invocation_rejected` | Reaches application; no authorization bypass |
| MCP `/healthz`, agent-signed | HTTP 404 HTML from Cloud Run frontend | Recorded separately; not used as a tool-chain pass |
| Vertex generateContent as actual runtime identity | HTTP 200, one candidate, `gemini-3.5-flash-lite`, global | Synthetic prompt; no personal data |
| Firestore reserved infrastructure document GET as runtime identity | HTTP 404 NOT_FOUND | Authorized database read, no contents disclosed or writes |
| Agent Engine tests in Linux Cloud Build | 61 passed, 14 warnings | External model/Calendar/credentials synthetic or fenced |
| Offline production MCP construction | Passed | Actual factory; no tool dispatch |

Backend's first resource-scoped query attempt saw IAM propagation denial. After
propagation, the same narrow resource policy passed testIamPermissions and real
query. The earlier unsuccessful project conditional binding was removed. Both
temporary deployer impersonation bindings (agent/backend) were removed after
smoke; only the required Google Reasoning Engine service-agent TokenCreator
binding remains on the agent identity. No service account key was created.

## Git and changed files

- Branch: `codex/gcp-recovery-deployment`.
- First contract-only commit pushed: `aea7fe4a93fac259ba9d1c8bbcc96014dc8c9acd`.
- Implementation commit: `7dff190f7df88e53ee31fb913bbbe4184aab3aca`.
- `git ls-remote origin refs/heads/codex/gcp-recovery-deployment` confirmed that
  exact implementation SHA after push. A subsequent documentation-only receipt
  commit contains this final handoff; its remote SHA is reported in the chat.
- Latest incorporated upstream: `0f52fdf3d147c52ef8fd73d2b818c2b6c02fd6cb`
  (a concurrent merge of the intermediate deployment branch into main).
- Product code base: `5af28ed916ba50dd687725e6e7634ad0ced199c3`.
- Build source code includes that upstream through merge `3ad0386d4e965fc68153c903257a2cc1dc4f6dca`.
  The later branch changes are deployment scripts/documentation, not product code.
- Changed files: `deploy/recovery-gcp.py`, `deploy/Dockerfile.recovery`,
  `deploy/check-recovery-composition.py`, `setup/RECOVERY-GCP-DEPLOY.md`, this handoff.
- Every push was preceded by staged diff review/check and secret-pattern scanning.
  Private settings, artifacts, credentials and logs remain excluded from Git.

## Ready for backend vs blocked activation

Ready: actual resource ID and operation contract, Vertex runtime identity/model,
private MCP origin/audience, backend get/query IAM, shared session Firestore DB,
pinned settings and reproducible deploy/update/check commands.

Blocked activation is an application integration dependency, not an unresolved
cloud permission failure: the deployed existing runtime/MCP are intentionally
bound to the inert infrastructure owner. The integration developer must complete
server-side per-user policy/source/Calendar resolution, preserve live-session and
exact approval grants, then update BOTH Engine and MCP with matching code/settings.
Changing the public website's transport alone is insufficient. Do not route live
users into the bootstrap owner or treat Google login as Calendar authorization.

Real MCP tool execution with an authorized live-user grant and the full
propose -> approval -> apply -> verify -> UI trace flow have not been run here.
Calendar authorization/resource sharing and a separately authorized test Calendar
are prerequisites for that next acceptance test. No common Calendar was assigned
to users, and no user Calendar/Moodle writes or user sessions were created.
