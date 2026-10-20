# Recovery website integration validation

Date: 2026-10-20. Delivery branch: `main`.
Tested source merge: `9c2d16d6a8164e382207cf3024a1ed566c2b67a9`.
It contains website integration `71bf31f` and Merey's final GCP branch `dca27c3`,
including previous main `0f52fdf`. Both workstreams merged without conflicts.
The final follow-up changes only documentation to record this verification.
No production deployment was performed by this integration session.

## Implemented

- Optional recovery composition in the existing Google/Moodle website, with
  explicit server-owned personal account and Calendar bindings.
- Custom Agent Engine `async_query(invocation, service_token)` transport, one
  attempt, strict result validation, ID token and cancellation/error cleanup.
- Moodle iCalendar UID planning, task effort, completion marks and concrete free
  windows; authenticated planning form and existing proposal/approval/trace UI.
- Transactional source freshness at consent and execution; exact plan approval,
  independently stored verification evidence and replay protection.
- Per-account Engine/MCP routing after service authentication. Unconfigured
  accounts retain the current calendar/chat application.
- Existing `icalendar==6.3.2` dependency included in the cloud extra/lock;
  all prior cloud package version pins were preserved.

## Verification

| Check | Result | Local evidence |
| --- | --- | --- |
| Full pytest on the final merged main, real local Firestore Emulator and portable PowerShell | **2069 passed**, 35 dependency deprecation warnings, no failures/skips; 173.56 seconds | `.validation/main-integrated-pytest.log`, `.xml` |
| Offline recovery evaluation on merged main | **347 passed**, 95 dependency warnings | `.validation/main-integrated-recovery.log` |
| Literal-loopback voice evaluation on merged main | **53 passed** | `.validation/main-integrated-voice.log` |
| Wheel and sdist | Both built successfully from merged main | `.validation/main-integrated-build.log` |
| Installed dependency consistency | `pip check`: no broken requirements | Terminal verification |
| Existing GCP composition check | `offline_mcp_composition_ok`; no tools dispatched; latest deploy script compiled | Terminal verification |
| Independent review | Four findings fixed; final review found no remaining actionable P0–P2 issues; 23 targeted tests passed | Review agent response |
| Whitespace and secret-pattern review | `git diff --check` clean; no credential/private-key patterns in intended changed source, tests and documents | Terminal verification |
| Native iOS tests | **Blocked before tests**: Xcode 26.3 `xcodebuild` exits 70 while loading `IDESimulatorFoundation`; missing symbol in installed `DVTDownloads` system framework | `.validation/main-integrated-ios.log` |

Reproduction uses the Python 3.12 test environment and README commands:

```sh
FIRESTORE_EMULATOR_HOST=127.0.0.1:8686 python -m pytest -q
python -I -S -B evals/recovery/run_evaluation.py --mode offline --output .validation/recovery-new-run
python -B evals/voice/run_checks.py --mode literal-loopback --report .validation/voice-new-run/report.json -- evals/voice -q
python -m pip check
python -m build --no-isolation
git diff --check
```

The full result includes 14 PowerShell tests using the existing portable launcher;
without a working `pwsh` on PATH those tests can skip. Recovery evaluation uses
a scripted model and synthetic Google adapters. The new website end-to-end tests
exercise the real application/runtime/MCP/solver/approval/verify code while
replacing external identity, provider and Calendar edges. These are not live
Gemini or personal Google Calendar acceptance tests.
The iOS toolchain failure is separate from the passing Python/voice protocol
checks; no native iOS test pass is claimed and no system Xcode components were changed.

## Browser check

A local-only fixture server composed the production application with synthetic
identity and data. The browser loaded `/planning`, saved a 90-minute task and a
concrete free interval, then reloaded and displayed both saved values. An initial
CSP defect that blocked the planning script was fixed with a regression test.
Screenshot: `.validation/recovery-planning-browser.png` (narrow viewport).
A separate desktop visual check and two-real-account production run were not
performed. No fixture or authentication bypass was added to product code.

## Review findings resolved

1. Stale planning could record approval before the final execution guard rejected
   the write. Consent now checks the current inputs/source in its transaction.
2. Changing the allowlisted Moodle user did not change the configuration revision.
   The revision now includes that binding, invalidating old sessions/plans.
3. The cloud extra omitted the parser used by the new feed path. Its existing
   pinned dependency and hashes are now in the cloud lock.
4. SDK cleanup could mask cancellation or leak a raw cleanup error. Primary
   cancellation/errors are preserved, cleanup-only failures are redacted.

The first broad run also exposed three legacy test configurations without the
optional application field; account binding now preserves that compatibility.
One pre-existing emulator contention test failed once, passed in isolation and
passed in the subsequent full runs, including the final merged main; its implementation was not changed.

## Deployment boundary

Merey's final GCP branch was fetched, merged into main and its committed resource receipts were read.
This session did not independently query or change GCP. The current Engine/MCP
bootstrap deployment must be updated with this final source and real private
account bindings before activating recovery on the website. See
[RECOVERY-WEB-HANDOFF.md](RECOVERY-WEB-HANDOFF.md) for the exact settings contract.

Remaining acceptance: same final wheel/settings on Engine and MCP, actual shared
personal test Calendar permission, website settings activation, two real users,
real model-initiated tools and an approved apply followed by independent verify
in that test Calendar. Automatic MyDU class-to-busy-window projection and personal
Calendar OAuth onboarding are outside this integration; free windows and explicit
Calendar sharing are used.
