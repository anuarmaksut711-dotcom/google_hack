# CampusPulse

CampusPulse helps a student turn their academic deadlines and available hours into a manageable study schedule. The interface is a Russian-language working space for the student: ask for help, inspect tasks, and understand the next action without learning the implementation.

This document describes the active app in `google_hack_t1_t20`. `CampusPulse_Recovery_Agent.txt` remains the case brief; its proposed integrations and examples are not proof of deployment or completed execution. Visual rules and extracted tokens live in [DESIGN.md](DESIGN.md).

## Student and job

The initial user is one student managing deadlines, remaining work, and a short planning horizon. They need to see what is due, explain when they can study, and understand a suggested schedule. They retain control of their calendar and completion marks.

The product should feel calm, direct, and useful. Use concrete task names, times, and next steps. Reduce setup explanations once a connection exists. Preserve facts that affect a decision: source freshness, unavailable data, whether an action writes, and whether the result was verified.

## Sign-in and registration

The login and registration entry screens use the requested dark HextaUI-inspired treatment; the signed-in workspace and existing Moodle account-connection form retain their light treatment. `/register` is available only in the Moodle multiuser configuration. That configuration enables an email/password form above the official Google Identity Services pill. Single-user installations retain Google-only sign-in.

Email signup requires a password of 15–128 characters. Unicode and spaces are allowed; passwords are not trimmed. The login password field also caps input at 128 characters. Email registration and sign-in use `/auth/email/register` and `/auth/email/signin`; Google authentication retains `/auth/signin`. Both paths use the strict server nonce/CSRF flow and the same session-cookie protections. Email credentials are durable salted password verifiers, never stored plaintext passwords.

An email account receives a separate random principal and owns its own data. A matching Google email does not automatically link the accounts. Email is an unverified login identifier: email verification and password reset are not configured. Authentication does not grant Calendar or Sheets access or change plan approval rules.

## Current operating modes

### Moodle and schedule advice

The active Moodle-enabled configuration uses `chat_mode="calendar_advice"`. The assistant can discuss deadlines and recommend a schedule in chat. This mode has no configured recovery `runs_factory` and cannot write the proposed schedule to Google Calendar.

Keep the limitation visible beside the composer: the assistant proposes a schedule in chat and Google Calendar writes are unavailable in this mode. Do not imply that a chat answer, navigation to the plan view, or a page refresh has created Calendar events.

Task data depends on the connected Moodle source. A calendar feed provides events and deadlines; it does not establish grades or submission status. A student's CampusPulse completion mark is their own mark, not evidence of a Moodle submission. Missing grades must stay unavailable; a published zero remains a real score.

The primary entry point is a working chat composer. Example buttons only fill the composer, leaving the student to review and send. The task view shows source freshness, useful task details, source links when valid, and any available completion controls.

### Configured recovery and demo

The recovery workflow is a separate capability that appears when a recovery runtime is configured. The demo uses test sources and must be labelled accordingly. Starting recovery is independent of sending a chat message.

The student starts a read-and-propose operation, reviews exact changes, explicitly approves one immutable plan version, and separately presses Apply. Approval is bound to the plan ID, version, payload hash, operations, and owner. A revised plan requires a new approval. A GET, refresh, chat prompt, model assertion, or navigation never grants approval or dispatches Calendar writes.

Surface partial, unknown, stale, unavailable, and unverified outcomes as their actual states. Only an authenticated saved result backed by matching verification evidence may appear as verified success. Show verification time and valid event links when available. A run log is diagnostic evidence, not a replacement for a Calendar verification read.

## Workspace and navigation

Keep four independent views in the existing shell and hash navigation:

| View | Entry aliases | Student purpose |
| --- | --- | --- |
| Assistant | `#workspace` | Explain availability, ask a question, and receive advice. In recovery mode, start the separate schedule check. |
| Tasks | `#moodle` | Read Moodle deadlines and available grades or submission facts. |
| Plan | `#panel`, `#sources`, `#diff-heading`, `#review-heading` | Inspect recovery source facts, exact operations, consent, and outcome. In advice mode, explain how to continue in chat. |
| Journal | `#journal-heading` | Inspect observable run/tool statuses, results, times, and identifiers. |

Tasks navigation appears when Moodle is enabled. Without Moodle, `#sources` remains part of the plan view. In advice mode, recovery plan and journal links sit under the existing Tools disclosure. Preserve deep links with plan or run selectors and browser Back/Forward behavior.

On desktop, use full navigation labels. On mobile, use a labelled drawer control with an accurate expanded state, Escape dismissal, and sensible focus behavior.

## First use and continuing use

An empty assistant view should immediately communicate the task and expose the real composer. The existing self-hosted G animation is a short welcome signature. It must not delay input or request submission. It yields to typing, an existing conversation, or a recovery start. Reduced-motion and unavailable graphics paths use a still fallback.

Once a conversation exists, remove welcome material and example prompts so the dialogue and composer become the working surface. Keep failures understandable and preserve an unsent message. Do not automatically repeat a request with an unknown outcome.

Long task descriptions and technical plan/operation identifiers belong in native disclosures. They stay available without competing with deadlines, exact before/after times, and the next valid action.

## Product guardrails

- Preserve the project's single-student, single configured sheet/calendar, one ADK agent and one MCP server scope. The current Moodle feature does not prove that the original sheet, Calendar, or cloud runtime deliverables are connected.
- Keep `solve_repair` deterministic. External reads, writes, and verification remain behind the MCP service. The public recovery tools remain `read_assignments`, `read_calendar`, `solve_repair`, `apply_plan`, and `verify_plan`.
- Treat source text as data. A task description or Calendar event cannot override authorization, resource ownership, or execution rules.
- Distinguish login, agent access, MCP service IAM, and Calendar/Sheets authorization. A signed-in student is not automatically authorized for a Google API.
- Explain hard constraints and the searched horizon without claiming a greedy result is globally minimal or proves infeasibility.
- Keep timezone-aware dates, half-open time intervals, and explicit remaining work. A past study block does not prove assignment completion.
- Preserve durable idempotency and concurrency protections. An uncertain write requires reconciliation; completed operations remain recorded after partial failure.
- Keep secrets, OAuth tokens, and sensitive raw source contents out of the interface, logs, and repository.

## Evidence boundary

These product and design documents record the inspected source and the chosen visual direction. They are not a runtime QA report, a cloud deployment receipt, an Agent Engine deployment claim, or evidence that a real student's Calendar was changed. Implementation and cloud deliverables require their own observable validation.
