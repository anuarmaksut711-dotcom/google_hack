---
name: CampusPulse
description: A calm study workspace with clear actions and visible student control.
colors:
  surround: "#f5f5f3"
  surface: "#fff"
  ink: "#252624"
  muted: "#666964"
  line: "#e7e8e4"
  subtle: "#f7f8f6"
  accent: "#37654a"
  selected: "#e8ebe6"
  focus: "#376d50"
  action: "#292d29"
  action-hover: "#41483f"
  warning-surface: "#faf5e9"
  warning-ink: "#805d25"
  error-surface: "#fcf0ed"
  error-ink: "#963f33"
  verified-surface: "#edf6ef"
  verified-ink: "#2d6240"
  auth-ground: "#111"
  auth-ink: "#f5f5f5"
  auth-muted: "#aaa"
  auth-focus: "#e0e0e0"
  auth-panel-start: "#252525"
  auth-panel-mid: "#1b1b1b"
  auth-panel-end: "#151515"
  auth-retry: "#373737"
  auth-retry-hover: "#444"
  auth-field: "#373737"
  auth-field-hover: "#3d3d3d"
  auth-placeholder: "#c3c3c3"
  auth-submit: "#414141"
  auth-submit-hover: "#505050"
typography:
  display:
    fontFamily: "'Segoe UI', system-ui, sans-serif"
    fontSize: "32px"
    fontWeight: 500
    lineHeight: 1.3
    letterSpacing: "-.035em"
  headline:
    fontFamily: "'Segoe UI', system-ui, sans-serif"
    fontSize: "21px"
    fontWeight: 600
    lineHeight: 1.4
    letterSpacing: "-.025em"
  body:
    fontFamily: "'Segoe UI', system-ui, sans-serif"
    fontSize: "14px"
    fontWeight: 400
    lineHeight: 1.65
  label:
    fontFamily: "'Segoe UI', system-ui, sans-serif"
    fontSize: "13px"
    fontWeight: 550
  technical:
    fontFamily: "Consolas, monospace"
    fontSize: "12px"
  auth-heading:
    fontFamily: "'Segoe UI', system-ui, sans-serif"
    fontSize: "28px"
    fontWeight: 600
    lineHeight: 1.3
    letterSpacing: "-.03em"
rounded:
  control: "8px"
  button: "9px"
  compact: "10px"
  panel: "12px"
  workspace: "14px"
  composer: "16px"
  auth-panel: "30px"
  auth-panel-mobile: "26px"
  auth-pill: "999px"
  auth-field: "18px"
spacing:
  xs: "4px"
  sm: "8px"
  md: "12px"
  lg: "16px"
  xl: "20px"
  section: "24px"
  wide: "30px"
components:
  button-primary:
    backgroundColor: "{colors.action}"
    textColor: "{colors.surface}"
    typography: "{typography.label}"
    rounded: "{rounded.button}"
    padding: "10px 16px"
  button-primary-hover:
    backgroundColor: "{colors.action-hover}"
  button-secondary:
    backgroundColor: "transparent"
    textColor: "{colors.muted}"
    rounded: "{rounded.button}"
    padding: "10px 16px"
  prompt-chip:
    backgroundColor: "{colors.surface}"
    textColor: "#5b6259"
    rounded: "{rounded.control}"
    padding: "7px 12px"
  navigation-current:
    backgroundColor: "{colors.selected}"
    textColor: "#262c26"
    rounded: "{rounded.control}"
    padding: "10px 14px"
  composer:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.ink}"
    rounded: "{rounded.composer}"
    padding: "18px 20px 12px"
  outcome:
    backgroundColor: "#f2f4f0"
    textColor: "{colors.ink}"
    rounded: "{rounded.panel}"
    padding: "22px 24px"
  outcome-verified:
    backgroundColor: "{colors.verified-surface}"
    textColor: "{colors.verified-ink}"
  outcome-warning:
    backgroundColor: "{colors.warning-surface}"
    textColor: "{colors.warning-ink}"
  outcome-error:
    backgroundColor: "{colors.error-surface}"
    textColor: "{colors.error-ink}"
  auth-card:
    textColor: "{colors.auth-ink}"
    rounded: "{rounded.auth-panel}"
    padding: "44px 40px 34px"
    width: "480px"
  auth-retry:
    backgroundColor: "{colors.auth-retry}"
    textColor: "{colors.auth-ink}"
    rounded: "{rounded.auth-pill}"
    height: "56px"
  auth-retry-hover:
    backgroundColor: "{colors.auth-retry-hover}"
  auth-field:
    backgroundColor: "{colors.auth-field}"
    textColor: "{colors.auth-ink}"
    rounded: "{rounded.auth-field}"
    padding: "0 22px"
    height: "60px"
  auth-email-submit:
    backgroundColor: "{colors.auth-submit}"
    textColor: "{colors.auth-ink}"
    rounded: "{rounded.auth-pill}"
    height: "60px"
  auth-email-submit-hover:
    backgroundColor: "{colors.auth-submit-hover}"
---

# Design System: CampusPulse

## Overview

**Creative North Star: "Operate"**

CampusPulse is a light, practical workspace inspired by the calm structure of the Codex desktop app. An off-white navigation surround frames a white inset working surface. The student should see a readable task, a useful next action, and their current state before technical detail.

The visual voice is quiet and direct. Segoe UI provides one consistent interface family; Consolas is reserved for code and identifiers. Charcoal controls carry actions, muted green carries focus and links, and restrained status tints explain outcomes. The existing animated G is the welcome signature; it does not become the color palette for the rest of the interface.

Login and registration are a deliberate, reference-driven exception: a dark charcoal ground, a softly directional panel, white headings, and a striped CSS orb. This treatment is scoped to the authentication page. The signed-in workspace and existing account-connection form keep their light styles.

**Key Characteristics:**

- Persistent labelled navigation and independent working views.
- White space and thin separators rather than decorative cards.
- A clear chat composer on first use and a focused dialogue thereafter.
- Native disclosures for long descriptions, identifiers, and supporting evidence.
- Status and consent expressed in words as well as color.

This system is extracted from `web/assets/styles.py`, `web/views/render.py`, `web/assets/client.py`, and `cloud/login.py`. Tokens above are normative. The component previews and motion/layout extensions live in `.impeccable/design.json`.

## Colors

The palette combines warm, nearly neutral surfaces with low-saturation green accents. Color supports orientation and state without competing with task information.

### Primary

- **Charcoal Action** (`action`, `action-hover`): submit, start, approve, and apply controls. Each control remains named for the action it actually performs.
- **Quiet Green** (`accent`): links and restrained interactive emphasis.
- **Focus Green** (`focus`): keyboard outlines across buttons, links, summaries, textareas, and focusable regions.

### Neutral

- **Workspace Surround** (`surround`): persistent sidebar and desktop outer frame.
- **Working White** (`surface`): main workspace, topbar, and composer.
- **Reading Ink** (`ink`): headings, primary labels, and body text.
- **Supporting Ink** (`muted`): source context, descriptions, hints, and non-primary controls.
- **Quiet Line** (`line`): separators, table edges, and secondary control boundaries.
- **Inset Surface** (`subtle`): hover states and supporting technical information.
- **Selected Surface** (`selected`): current navigation and subtle selected states.

Status pairs have semantic meanings: verified green is reserved for a backed verification result; warm warning covers unknown, partial, stale, needs-input, and applied-but-unverified states; error red covers failed verification or insufficient searched capacity. Keep the status title and explanation visible so color never carries the meaning alone.

**The Evidence Color Rule.** Green success styling requires the same verification evidence as a success claim in text. A loaded page, completed chat response, or populated journal does not qualify.

### Authentication exception

The `auth-*` tokens belong only to the dark login/registration surface. The panel gradient runs at (105deg), from the start color through the middle color at (42%) to the end color. Use white-toned heading text, muted supporting copy, charcoal email/password fields, and a pale keyboard outline. Keep the official Google Identity Services `filled_black` pill; do not imitate its provider-rendered content. These reference-pinned colors do not replace workspace tokens.

## Typography

**Display and Body Font:** Segoe UI, with local system and sans-serif fallbacks.

**Technical Font:** Consolas, with monospace fallback, only for IDs, hashes, JSON, and code.

**Character:** One familiar interface family keeps the workspace coherent and reduces visual noise. Weight and spacing establish hierarchy. Do not load web fonts.

### Hierarchy

- **Display:** The frontmatter display role is the empty-assistant heading. On mobile it scales down to (27px), with tracking (-.03em).
- **Headline:** The headline role identifies view sections and outcome titles. Mobile section headings use (19px).
- **Body:** The body role is the document baseline. Task and plan content commonly uses (13px); descriptions stay within approximately (75ch).
- **Label:** Action labels use the frontmatter label role. Navigation uses (13px), with current items at weight (600). Status, metadata, and disclosure summaries commonly use (12px).
- **Composer:** Input uses (15px) with line height (1.7), rising to (16px) on mobile. Dialogue text uses a more open line height (1.85).
- **Technical:** Technical data stays visually subordinate and wraps without clipping. Tables use tabular numerals for comparison.
- **Authentication heading:** Use the dedicated auth-heading role, reduced to (26px) on mobile; the secondary title is (18px) at weight (500). Supporting authentication copy remains in Segoe UI.

**The One Interface Voice Rule.** Keep student-facing text in Segoe UI. Reserve monospace for the contents of technical fields, not for navigation, labels, dates, or marketing-like headings.

## Layout

The desktop shell uses a fixed sidebar (236px) and an inset white workspace with outer gaps (10px). The workspace has a soft edge and a sticky topbar with minimum height (76px). Main content is centered within a maximum width (1140px), with horizontal padding (44px).

The assistant is a vertical working surface. A short introduction and G mark lead to a composer capped at (720px). Example prompts follow in a wrapping row. Dialogue uses the same comfortable width. When chat history exists, the introductory block and examples disappear, while the composer remains available beneath the conversation.

Tasks use full-width separated rows: subject, readable title, deadline/status facts, optional description disclosure, and relevant actions. Recovery views use a concise outcome area, source facts, an exact before/after table, and distinct approval/application controls. Journal entries are ordered disclosures. The four views share the shell but do not stack as one long dashboard.

At viewport width (1100px) and below, the sidebar narrows to (212px), main padding becomes (30px), and the topbar subtitle hides. Full navigation labels remain visible. At (760px) and below, the sidebar becomes a labelled drawer (260px), the white workspace fills the viewport, main horizontal padding becomes (22px), source columns stack, and action groups stack where needed. The drawer closes through its control, outside interaction, Escape, or navigation.

On short desktop viewports, the welcome mark scales down to (170px). The usual desktop mark is (250px), while mobile uses (200px). Do not increase the introduction so much that it hides the composer and first actions.

**The Independent View Rule.** Route through the existing hash aliases, including browser history and deep links. Navigation changes the working view without starting a run, submitting chat, or granting consent.

Authentication uses a centered standalone panel capped at (480px), without the workspace sidebar. At (520px) and below, its padding becomes (36px 24px 30px), and its radius uses `auth-panel-mobile`.

## Elevation & Depth

The workspace is flat, with no resting drop shadows. Surface tones, thin borders, spacing, and the fixed sidebar/sticky topbar establish hierarchy. The composer gains a quieter green border on focus-within; keyboard focus also has a clear outline (2px) offset from the focused element (4px). The authentication exception uses a diffuse panel shadow and inset shading on the striped orb; exact values live in the sidecar.

**The Flat Surface Rule.** Use tonal separation and edges before introducing elevation. Keep feedback noticeable through text, semantic tint, and position; do not turn every data group into a floating card.

## Shapes

Use gently rounded rectangular controls and panels. The radius scale above separates compact controls, buttons, outcomes, the workspace edge, and the composer. The chat submit control is compact and square; its accessible label supplies the action name. Current-step indicators are small circles; they are the exception to the rectangular form language.

Authentication adds a generously rounded panel, pill action controls, and input fields using the `auth-field` radius. Its decorative orb is circular (64px), reduced to (58px) on mobile, with static diagonal stripes and tonal shading.

Tables retain visible cell relationships and scroll horizontally within their own region when necessary. Long text wraps within a row or disclosure rather than stretching the page. Keep icon strokes thin and consistent; icons accompany full text labels on desktop.

## Components

### Buttons

Confident, compact, and named for a concrete action. Primary buttons use the charcoal action tokens with white text, a minimum height (42px), and the frontmatter padding/radius. Hover shifts the surface; active darkens it. Disabled controls use a quiet grey treatment and a default cursor. Keyboard outlines remain visible.

Secondary and revoke/journal controls use a transparent background, muted text, and a quiet border where present. Hover adds the inset surface. Do not give Approve and Apply one combined button or reuse their labels for chat submission.

### Prompt chips

Quiet examples rather than automatic actions. Use small outlined rounded rectangles, minimum height (36px), and compact text. Clicking one fills and focuses the composer; it never sends the message. Hover changes the surface and border without motion displacement.

### Composer and dialogue

The composer is a white bordered container with a visible editable field, accessible label, send control, keyboard hint, and concise availability/status line. Enter sends; Shift+Enter adds a line. Respect composition input and disabled/working states. Preserve text after request errors.

User messages use a pale rounded bubble aligned to the right; assistant messages remain plain on the working surface. Request metadata is available through a native disclosure. Chat availability and calendar-advice limitations stay truthful to the configured mode.

### Navigation

Navigation rows have consistent icon/text alignment, a minimum height (44px), and a muted selected fill. Full labels remain visible in desktop layouts. The current row has an `aria-current` value. The mobile drawer control exposes `aria-expanded`; Escape restores focus to the control. Tools in advice mode remain grouped in a native disclosure.

### Outcomes and containers

Outcome containers use the panel radius, modest padding, a concise heading, and a short explanation. Semantic tint supplements the label. Maintain the distinction between proposed, approved, applying, partial, unknown, stale, applied-unverified, failed verification, and verified states.

### Tasks and disclosures

Tasks use ruled rows rather than elevated cards. Keep subject context quiet and task names readable. Native details/summary controls hold long descriptions, past deadlines, and student-marked completed items. Submission facts and student completion marks retain their different labels.

Plan identity and operation details also use native disclosures. Keep the exact before/after time changes visible during review; placing identifiers behind disclosure must not conceal which operations the student approves. Summaries remain keyboard reachable and have clear hover/focus treatments.

### Welcome G

Keep the existing self-hosted GSAP/Three welcome asset as a one-shot animation. It is decorative and marked as such for assistive technology. The first edit, an existing dialogue, or starting recovery dismisses it. Pause work when it is not visible, and settle to a still for reduced motion. Preserve the image fallback when graphics cannot initialize. Never block input on animation completion.

### Authentication gate

Use the scoped dark panel for both login and registration. In Moodle multiuser mode, the sequence is the decorative CSS orb, CampusPulse heading, concise mode title, email and password fields, email submit action, official Google control, status line, and mode-switch link. Single-user installations show Google sign-in without the email/password form. The existing light account-connection form is a separate surface.

Fields are full-width and (60px) high, with muted placeholders, hidden accessible labels, a subtle hover fill, and an offset keyboard outline. The signup password hint states the 15–128 character rule and permits spaces; preserve Unicode and the exact password value. The login field caps input at 128 characters. Keep the full-width email submit pill, busy/disabled states, and readable field/request errors. Do not introduce a verified-email badge or a password-reset link for unconfigured capabilities.

The Google control retains the official `filled_black`, `pill`, `large` configuration below the form. Google unavailability must leave available email sign-in usable. Keep loading, working, error, and retry states readable; the retry control is a full-width pill. Sign-in, registration, and provider choice do not change plan approval semantics.

## Do's and Don'ts

### Do:

- **Do** lead each view with its actual student task and next valid action.
- **Do** keep the composer and example controls available on an empty assistant view.
- **Do** use plain source freshness, error, and limitation text near the relevant content.
- **Do** keep approval tied to the exact saved plan version and Apply as a separate action.
- **Do** expose observable tool/run status, timings, and IDs through readable disclosures.
- **Do** honor reduced motion and maintain keyboard access to every action and disclosure.

### Don't:

- **Don't** introduce web fonts, dark signed-in workspace surfaces, or a multicolor application palette. Keep the reference-pinned dark gradient scoped to authentication.
- **Don't** turn the welcome logo into a repeated or blocking effect.
- **Don't** replace labelled desktop navigation with an icon-only rail.
- **Don't** auto-submit an example, auto-apply a plan, or imply that a refresh writes to Calendar.
- **Don't** show verified success from an unverified or unknown outcome.
- **Don't** treat hidden technical details as permission to conceal exact proposed changes.
