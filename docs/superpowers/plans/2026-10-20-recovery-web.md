# Recovery Website Integration Implementation Plan

> **For agentic workers:** Execute inline with superpowers:executing-plans; use test-first verification for each slice.

**Goal:** Make the existing recovery workflow available to explicitly bound Google users in the personal Moodle website.
**Architecture:** Server-configured per-user bindings compose existing policies and workflows; custom Agent Engine transport forwards only private grants. Existing calendar/chat remains the default when unconfigured.
**Tech Stack:** Python 3.12, FastAPI, Pydantic, Google ADK/Vertex SDK, MCP, Firestore, existing HTML/JS.
**Spec:** docs/superpowers/specs/2026-10-20-recovery-web-design.md

## Global Constraints
- Keep async_query(invocation, service_token), status_code/payload and exact-version approval.
- Reuse the existing dependencies (including the existing calendar extra in the cloud lock); no browser resource selection, no implicit shared Calendar, no blind apply retries.
- No infrastructure deployment or writes to real student resources in this task.

## Review Focus
- Revoked/changed sessions cannot use old grants or cross account boundaries.
- ICS UIDs, stale/error feeds, completion marks and changed input survive transaction checks.
- SDK timeout/cancellation/invalid envelopes never become confirmed success or retry.
- Unconfigured users and existing chat/feed flows retain current behavior.
- UI distinguishes advice, proposal, approval and independently verified application.

### Task 1: Bound custom-operation transport
- [x] Tests: SDK receives exact grant/token, rejects invalid target/envelope, one attempt on failure, closes on cancellation.
- [x] Implement cloud/engine_transport.py using the pinned SDK and explicit target.
- [x] Run focused tests and preserve existing engine/runtime tests.

### Task 2: Feed planning and trusted account routing
- [x] Tests: bound accounts select separate policies; duplicate calendars rejected; UID estimates/completion/freshness/transactions.
- [x] Extend CloudSettings and planning model ownership; add cloud/accounts.py and lms/feed_planning.py.
- [x] Select per-account worker/MCP only after service authentication; preserve existing single-account mode.
- [x] Run account/planning and old security/approval tests.

### Task 3: Website composition and planning UI
- [x] Tests: real cloud app exposes run/planning for configured users, isolates two accounts and blocks unknown users; defaults unchanged.
- [x] Compose existing backend/run APIs into multiuser app with clock/policy consistency.
- [x] Add authenticated planning form and reuse recovery/trace controls; review visible copy and verify narrow browser form save/reload. A separate desktop visual pass was not performed.
- [x] Test local end-to-end recovery through custom runtime with synthetic external edges.

### Task 4: Verify and hand off
- [x] Full pytest with Firestore Emulator, recovery/voice evaluations, packaging and git diff check.
- [x] Independent review of account authority, transaction freshness and ambiguous writes; fix blocking findings.
- [x] Document exact GCP configuration/contract for Merey.
- [ ] Commit and push integration branch for cross-team visibility. Automatic approval review rejected this export because the human's explicit push instruction named Merey's work. Local implementation is complete; waiting for explicit approval of this branch's push.

Final results and limits: setup/RECOVERY-WEB-VALIDATION.md.
