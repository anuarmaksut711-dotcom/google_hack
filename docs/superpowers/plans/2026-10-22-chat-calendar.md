# Chat timetable and Calendar Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Review and save a timetable supplied in chat, publish classes and study blocks to Google Calendar after exact approval, and use saved classes in future plans.

**Architecture:** Extend the structured Gemini reply with an optional typed weekly draft. Persist drafts separately from the authoritative saved week and prefill the existing authenticated editor. Extend the Calendar writer with server-derived class proposals while preserving study constraints and existing ownership, approval and verification boundaries.

**Tech Stack:** Python, FastAPI, Pydantic, ADK Gemini, Firestore, MCP, Google Calendar, inline JavaScript, Vertex AI Agent Engine, Cloud Run.

**Spec:** `docs/superpowers/specs/2026-10-22-chat-calendar.md`

## Global Constraints

- Classes and study blocks are added only after preview and confirmation.
- Timetable dates are explicit; at most 30 weekly classes and a period of at most 91 days.
- Study proposals retain the 72-hour limit and class/Calendar conflict checks.
- Preserve existing cloud identities, IAM, secrets and OAuth mounts.
- No automatic replay of Calendar mutations; final status requires independent verification.

## Review Focus

- Missing dates and partial-week text must remain reviewable drafts.
- Cross-owner draft IDs and expired/revoked sessions must fail closed.
- Late responses must not overwrite a newer editor session or draft.
- Calendar recurrence must end on the reviewed date in the account timezone.
- Repeated apply requests must reconcile the same events without duplicates.

---

### Task 1: Calendar connection

**Files:** `web/google_calendar.py`, `deploy/deploy-calendar.ps1`, focused Calendar connection and deployment tests.

**Interfaces:** Existing `calendarPost(path, value)`; authenticate through `/auth/session` for ordinary operations and preserve prelogin challenge handling.

- [x] Write and run a failing cookie-less authenticated-session test.
- [x] Bootstrap CSRF, bind the document session and reject session switches without replaying POST.
- [x] Preserve existing OAuth environment and secret mounts in the deployment script; run focused tests.

### Task 2: Chat draft and weekly editor

**Files:** `domain/chat_plan.py`, new `domain/chat_schedule.py`, `agent/conversation.py`, `web/chat.py`, `web/manual_schedule.py`, `lms/manual_schedule.py`, and focused tests.

**Interfaces:** `AdviceReply(kind='classes', timetable=...)`; `ManualSchedule.save_draft(context, request_key, draft)` returns an opaque ID; `read_draft(context, id)` returns a validated draft. `app.state.class_schedule_drafter` stores drafts from chat. The editor reads `/api/account/classes/drafts/{id}` and requires review before its ordinary save.

- [x] Test a chat draft with absent dates, invalid/overlapping times and no writes to the saved week.
- [x] Add strict typed output and prompt instructions; save owner-bound idempotent drafts separately.
- [x] Render a chat link that prefills the existing editor; test ownership and session guards.
- [x] Require full-week/date review, then expose the explicit Calendar proposal button.
- [x] Run domain, chat, weekly schedule, import and editor tests.

### Task 3: Class Calendar proposals

**Files:** `integrations/connected_plans.py`, `integrations/local_calendar.py`, `cloud/connected_calendar.py`, `cloud/connected_calendar_mcp.py`, `web/calendar_plans.py`, and focused tests.

**Interfaces:** `POST /api/google-calendar/plans/classes` derives the proposal from the saved week. Existing list/approve/apply/verify APIs remain the publication boundary. Owner/session-bound grants carry the bounded full-period read horizon.

- [x] Test weekly recurrence, explicit period, full-period conflicts and exact approval.
- [x] Implement server-derived class proposals and actual Google recurrence verification.
- [x] Test duplicate apply, session revocation, changed saved schedule and unknown write outcomes.
- [x] Label class and study previews clearly and run all focused Calendar tests.

### Task 4: Verified release and presentation

**Files:** Release documentation, `presentation/pitch-2026-10-22/`, existing build/deployment scripts.

- [ ] Review integrated changes, run complete required checks and commit/push the exact source. Local full run: 2549 passed; final changed-code focused run: 95 passed. Independent review found no remaining actionable issue.
- [ ] Run the immutable Linux cloud build and verify wheel/image/check receipts.
- [ ] Update existing MCP service, Agent Engine and website without IAM/config changes.
- [ ] Exercise the actual Moodle → visible tool calls → plan and chat timetable → review → Calendar scenario.
- [ ] Update receipt, deck, speech and demo script from observed evidence; render every slide.
- [ ] Push final documentation and report exact source, deployment and any remaining user-required OAuth action truthfully.
