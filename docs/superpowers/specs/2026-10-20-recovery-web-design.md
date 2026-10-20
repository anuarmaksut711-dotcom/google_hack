# Recovery in the personal Moodle website

The approved task connects the existing recovery workflow to the deployed multiuser calendar website. GCP deployment is owned by Merey's separate branch. Reuse RecoveryRuns, exact approval, grants, MCP readers/writers, solver, verify and trace UI. Preserve the default calendar/chat application when recovery is unconfigured.

## Authority and account selection

Add a bounded server-configured `calendar_feed_accounts` list to CloudSettings. Each entry binds an immutable Google principal, an expected Moodle user ID and one explicit Calendar ID shared with the MCP service identity. Derive the existing google-hash owner namespace on the server. Reject duplicate principals/calendars. Browser/model input never selects a Calendar or an account. Users without an entry keep the current application and cannot invoke recovery. Personal Calendar OAuth is outside this task; explicit resource sharing is already the project's access model.

Backend, Agent Engine and MCP consume identical validated settings. At private ingress verify the service token before reading a grant. Select the configured account by its server-owned grant binding, then retain the existing full session/config/resource/grant validation. No arbitrary settings travel in the query.

## Data and workflow

A feed planning adapter reads the current owner's existing durable CloudCalendarFeed inside the existing planning/approval transactions. Accept real ICS UIDs as task selectors. Never convert a UID to a Moodle assignment ID or invent submission status/effort. Explicit local completion excludes a task; missing effort/windows yields needs_input. Wrong feed binding, missing/stale cache or changed input blocks dispatch/approval. Reuse MoodleAssignmentsReader and transactional freshness guards.

The multiuser composition injects a CloudBackend using the custom AgentEngineTransport, includes run/planning APIs and reuses the existing run/approval/trace controls. Add an authenticated planning page for task estimates and explicit free intervals. Chat remains advisory; wording directs configured users to the separate recovery action.

## Transport

Custom operation only: async_query(invocation, service_token), response {status_code, payload}. Platform OAuth and backend service ID token are separate. Explicit target and token audience, bounded response/time, one attempt, cancellation propagation, cleanup and redacted errors. Never parse model text as evidence or retry ambiguous writes.

## Validation and delivery

Tests cover two-user isolation, unknown users, wrong service token before grant reads, per-account source selection, actual ICS UIDs/completion/freshness, planning revision conflicts, custom SDK protocol/no retry, existing propose/approval/apply/verify/replay, UI setup/trace and unchanged no-recovery behavior. Run complete pytest with Firestore Emulator, offline evaluations and build. Browser proof uses synthetic identities/providers; production proof requires Merey's deployed resources and a separately authorized test Calendar run.
