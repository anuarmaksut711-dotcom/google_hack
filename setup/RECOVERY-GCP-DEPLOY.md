# Recovery infrastructure deployment

This runbook targets only `gen-lang-client-0602946419`, `us-central1`.
It never deploys or changes the public `campuspulse` service, billing linkage,
Calendar ACLs, user calendars, Moodle records or user sessions.
Use an authenticated deployer with permission to provision the listed resources.
No service account keys are needed. See [handoff](RECOVERY-GCP-HANDOFF.md).

## Environment and inspection

Use Python 3.12, the repository's hash-pinned `requirements/cloud-linux.lock` and
`requirements/dev.lock`. Agent deployment additionally verifies Vertex SDK
1.165.1. Run from the repository root:

```sh
git fetch origin
git log -3 origin/main --oneline
python deploy/recovery-gcp.py inspect
```

Inspect project identity, existing engines/services, accounts, buckets and billing
before provisioning. `provision` reuses existing named resources, preserves IAM
bindings and checks the existing session database instead of making a second DB.
It does not link billing. Existing disabled accounts are not re-enabled.

```sh
python deploy/recovery-gcp.py provision
python deploy/recovery-gcp.py bootstrap-settings
```

`bootstrap-settings` reads only the existing calendar app settings, substitutes an
inert reserved owner with nonexistent source/Calendar IDs, then stores the validated
configuration in ignored `.campuspulse-local/recovery-gcp/engine-settings.json` and
Secret Manager. It refuses to overwrite a file or enabled secret version.
The reserved owner must never be used for real users; per-user integration must
replace this configuration before enabling recovery in the website.
No Google subject, Calendar ID, feed URL or user credentials are copied to Git.

The MCP configuration version is explicitly pinned to `:1` in the initial deploy.
For future configurations, validate settings, create a new Secret Manager version
and explicitly review/update the pinned version; do not silently replace `latest`.

## Build and private MCP

```sh
python deploy/recovery-gcp.py build-mcp
gcloud builds describe BUILD_ID --region=us-central1 --project=gen-lang-client-0602946419
```

Wait for `SUCCESS`, inspect `results.images`, and deploy the returned immutable
`recovery-mcp@sha256:...` image, never an unverified tag:

```sh
python deploy/recovery-gcp.py deploy-mcp --image IMMUTABLE_IMAGE
python deploy/recovery-gcp.py check-mcp
```

The source upload has an allowlist: product source/assets, test Python files,
the pinned locks, license, pyproject and deployment files. No ignored config,
logs, credentials or copied repositories are uploaded. The multi-stage image
runs the existing 61 Agent Engine tests under Linux before producing the wheel.
Tests do not use external model/Calendar/credentials. The runtime image excludes
the test suite and dev environment. Preserve the immutable image/build receipts.

MCP ingress is network reachable because Agent Engine uses its HTTPS endpoint,
but Cloud Run IAM allows only the agent identity, not anonymous/authenticated-all
principals. Application checks additionally require its signed ID token and an
existing session-bound MCP grant. `/healthz` is not a tool-chain smoke.

## Agent Engine

```sh
python -m build --no-isolation --wheel
python deploy/prepare-agent-engine.py --settings .campuspulse-local/recovery-gcp/engine-settings.json --wheel dist/campuspulse_recovery-0.1.0-py3-none-any.whl --requirements requirements/cloud-linux.lock --output .campuspulse-local/recovery-gcp/bundle
python deploy/recovery-gcp.py deploy-engine --bundle .campuspulse-local/recovery-gcp/bundle
```

The prepare command remains offline and reports `prepared_not_deployed`.
The separate deploy command validates manifest hashes, checks for an existing
`campuspulse-recovery` engine, uses the actual staging bucket and pinned SDK
`vertexai.agent_engines.create`, then waits for cloud deployment completion.
It does not replace an existing engine on rerun: inspect and deliberately plan
an update if one exists. A failed/timed-out create must be reconciled using the
regional engine/operation list before another create.

```sh
python deploy/recovery-gcp.py authorize-backend --resource ACTUAL_RESOURCE_ID
python deploy/recovery-gcp.py check --resource ACTUAL_RESOURCE_ID
python deploy/recovery-gcp.py model-smoke
```

`authorize-backend` creates/reuses a two-permission custom role (`get`, `query`)
with a condition restricted to the exact engine resource. It does not grant
backend project-wide Vertex administrator/user access.

The check reads actual remote operation schema and calls `async_query` with
deliberately invalid synthetic input, expecting the runtime's 403 envelope.
This proves remote custom-operation dispatch and rejection, not successful
recovery or tool execution. `model-smoke` separately makes one synthetic Vertex
request as the agent service account. `check-mcp` checks anonymous denial,
signed-agent health and signed requests denied without grants.

Local identity impersonation requires `iam.serviceAccounts.getAccessToken` /
`getOpenIdToken` on the specific account. If unavailable, use a narrowly scoped,
time-limited deployer `roles/iam.serviceAccountTokenCreator` binding for smoke
and remove that binding afterwards. Do not grant the deployer key creation.
Runtime-to-MCP authentication uses platform credentials, not that temporary grant.

## Acceptance boundary

Keep grants, session revocation, exact plan hash/version approval, resource
ownership, no-blind-write-retry, independent verify and redacted journal intact.
Before user-facing activation, the parallel backend integration must provide
server-resolved per-user sources/Calendar and the matching runtime/MCP binding.
Google login alone provides no Calendar authorization. Explicit resource sharing
or separate per-user OAuth authorization is required; never assign one common
Calendar to every user. Full propose/approval/apply/verify acceptance is separate
from infrastructure smoke and requires a separately authorized test calendar.

References: [Google deployment](https://docs.cloud.google.com/gemini-enterprise-agent-platform/scale/runtime/deploy-an-agent),
[existing runtime contract](AGENT-ENGINE.md). Runtime operation compatibility is
verified against the repository's pinned SDK, not inferred from newer docs.
