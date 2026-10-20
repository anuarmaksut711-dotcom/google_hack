# Recovery website integration handoff

Delivery branch: `main`. Website integration from `codex/recovery-web-integration`
(`71bf31f`) and Merey's final `codex/gcp-recovery-deployment` (`dca27c3`) were
combined in source merge `9c2d16d`. This document describes the implemented code
contract; it is not a claim of deployment or a passed cloud smoke.

## Shared private operation

`async_query(invocation, service_token)` returns `{status_code, payload}`.
`invocation` is the existing 43-character server grant, never browser/model text.
The backend sends a signed ID token with audience `CloudSettings.agent_origin`;
platform OAuth for Agent Engine is separate. Backend transport performs one
attempt, preserves ambiguous outcomes and closes its SDK client.

## Personal accounts

All components must use the same CloudSettings, Firestore project/database and
identities. Add the server-owned allowlist `calendar_feed_accounts`:

```json
[
  {
    "principal_id": "https://accounts.google.com#REAL_IMMUTABLE_GOOGLE_SUB",
    "moodle_user_id": 42,
    "calendar_id": "REAL_EXPLICIT_PERSONAL_TEST_CALENDAR_ID"
  }
]
```

The values above are placeholders, not live bindings. Use the verified Google
`sub`, not an email. Calendar IDs, principals and Moodle users must be unique.
Owner IDs are derived as `google-` plus SHA256(principal). The Moodle URL/token
stays in the existing private account record and never enters these settings.
Do not configure REST `moodle` together with `calendar_feed_accounts`.

MCP accesses each explicit Calendar through the existing resource-sharing model:
the owner must share that dedicated Calendar with the MCP service identity.
Google Sign-In grants no Calendar permission. No fallback to `primary` or a
common calendar is allowed. Users without an allowlist entry retain calendar/chat
and cannot run recovery. Changing bindings invalidates old sessions/plans.

## Website switch

`CalendarSettings.recovery` is optional. When enabled it contains:

- `settings`: the shared CloudSettings described above (Vertex chat provider for
  the recovery runtime, backend_origin equals the public website origin).
- `target`: `{project_id, location, resource_name}` for the deployed custom agent.

The calendar/chat application's existing `chat` settings can remain Gemini API.
Both application settings must refer to the same Firestore project/database;
recovery service identities are supplied by the nested CloudSettings.

Deploy Agent Engine and MCP with the integration-capable wheel/settings before
enabling the website switch. The integration does not create infrastructure,
authorize personal resources automatically, or replace existing chat behavior.

## Merge/deployment coordination

Use the final `main` before rollout; both workstreams are incorporated there.
Infrastructure changes live in `deploy/` and their own GCP handoff document.
Do not overwrite application code from an older wheel. Build the final merged source once and pin its image/wheel
hash in deployment receipts. Validate operation schema and run read-only checks
before any separately authorized apply/verify in a dedicated test Calendar.

## GCP work received during integration

Merged `codex/gcp-recovery-deployment` at `dca27c3`, including the earlier
`origin/main` `0f52fdf`. Merey's committed GCP handoff reports:

- Engine: `projects/585736102295/locations/us-central1/reasoningEngines/3019439524657758208`.
- MCP: `https://campuspulse-recovery-mcp-585736102295.us-central1.run.app`.
- Agent audience: `https://campuspulse-recovery-agent-585736102295.us-central1.run.app`.
- Backend identity: `campuspulse-calendar@gen-lang-client-0602946419.iam.gserviceaccount.com`.
- Agent/MCP identities: `campuspulse-recovery-agent@...` / `campuspulse-recovery-mcp@...`
  in project `gen-lang-client-0602946419`.
- Existing deployment is bound to an inert bootstrap owner; it must be updated
  with this code and explicit real account bindings before activating the site.

These are repository handoff receipts, not a new cloud verification from this
integration session. See RECOVERY-GCP-HANDOFF.md and RECOVERY-GCP-DEPLOY.md.

## Creating the final private website settings

Keep both source files outside Git. The engine settings must already contain
real `calendar_feed_accounts`, explicit shared Calendars and the common Firestore
project/database. Example merge (replace private file paths, not credentials):

```python
import json
from pathlib import Path
from campuspulse_recovery.cloud.calendar_app import CalendarSettings

calendar = json.loads(Path('PRIVATE/calendar-settings.json').read_text())
engine = json.loads(Path('PRIVATE/engine-settings.json').read_text())
calendar['recovery'] = {
    'settings': engine,
    'target': {
        'project_id': 'gen-lang-client-0602946419',
        'location': 'us-central1',
        'resource_name': 'projects/585736102295/locations/us-central1/reasoningEngines/3019439524657758208',
    },
}
validated = CalendarSettings.model_validate(calendar)
Path('PRIVATE/calendar-with-recovery.json').write_text(validated.model_dump_json())
```

Pin a NEW settings secret version; retain the current version for rollback.
Install the same final wheel on Engine/MCP and build the website image from the
same merged code. Cloud runtime lock now includes `icalendar==6.3.2`; the resolver
confirmed all other cloud pins unchanged. Update the existing Engine with
`deploy/recovery-gcp.py update-engine`, rather than creating another resource.
Use the existing GCP scripts for the corresponding MCP update; then deploy the
website's new pinned settings. Never enable the website switch against the old
bootstrap-only runtime. Shared Calendar authorization must be checked separately.

## Acceptance checks

- `/planning` saves task effort and concrete free windows under optimistic revision.
- `/api/runs` uses the private custom operation; `/api/approval` checks current
  source/input inside the consent transaction.
- A change of effort, completion or feed rejects old consent; changing the
  allowlisted Moodle user changes the session/grant config revision.
- `/api/runs/continue` requires exact approval; replay does not create extra writes.
- `/api/runs/{id}/trace` is owner/session-scoped, and `/ui/state` uses independently
  stored verify evidence rather than chat text.
- Export must be Moodle All events + Recent and next 60 days for recovery.
  Narrower/unknown export ranges remain usable for chat but cannot claim a
  complete 72-hour recovery source.
- Users without a server entry retain the existing app. Enrolling/changing a
  binding requires sign-in again; no public endpoint accepts Calendar selectors.

No production settings or personal Google subject/Calendar IDs were invented or
committed. Final production activation still requires the actual private user
bindings and a separately authorized test Calendar run.
