# Recovery GCP handoff

Status: Agent Engine created and private MCP Ready; final configuration update
and API smoke are in progress. Full per-user recovery is not enabled.
Project: `gen-lang-client-0602946419` (`585736102295`); region: `us-central1`.
The existing public `campuspulse` calendar/chat service must remain unchanged.

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
  get/query permissions conditioned on this exact engine resource.

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

## Acceptance evidence to append

Record actual engine ID, operation schema, immutable MCP image, private IAM,
service identities, Vertex model/location, safe API smoke, checks and remote SHA.
Offline bundle, Cloud Run health, denied calls and full recovery success are
separate evidence categories. No authorization checks may be bypassed for smoke.

## Current verification boundary

Create operation `6579780921266798592` completed without error. Configuration
update operation `7409287677633101824` is pending as of this intermediate commit.
Remote schema contains exactly one async method, `async_query`, with two required
string parameters `invocation` and `service_token`. No `async_stream_query` adapter
was substituted. Firestore GET of a reserved nonexistent infrastructure document
as the runtime identity returned NOT_FOUND (authorized read, zero writes).

The first MCP revision failed because bootstrap used an eight-column assignments
range; the existing reader requires six. Secret version 2 corrects that range;
MCP revision 2 is Ready. The same corrected settings are being applied to the
existing Agent Engine, without creating a duplicate resource.

Windows initially had missing test dependencies and a network-fence/socketpair
conflict. The authoritative Linux Cloud Build run completed all 61 tests. A new
offline composition check also constructs the actual production MCP factory
without dispatching tools or relaxing authorization.

The website was not deployed by this branch. Another concurrent deployment changed
its revision during this work; the observed `campuspulse-00005-2lj` still returns
healthy Firestore/Gemini configuration. Do not roll back concurrent website work.
