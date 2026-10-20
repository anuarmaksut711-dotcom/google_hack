# CampusPulse visual system

The user-selected references replace the previous light workspace. Composition follows the supplied Codex screenshot; color follows the supplied dark Gemini screenshot. This is an Operate surface.

## Shell and hierarchy

A 72px activity rail and 328px labelled sidebar frame the full-height workspace: at the reference 1920px width, the content begins at x=400. The rail duplicates real navigation destinations and owns account connection/sign-out. The sidebar owns existing sections, actual user-message entries and the CampusPulse project entry. No fabricated chat titles are shown. On mobile it becomes the existing accessible drawer.

The empty conversation follows the supplied reference geometry: a saturated four-color G above the centered heading, with a 264×198px canvas (about 122px visible glyph), 216×162px on short desktops, and 192×144px on mobile (160×120px below 640px height), no visible explanatory paragraph, and a 1016px composer at the bottom. Context links and input share one continuous rounded surface with one outer focus ring. There is no visible Enter/Shift+Enter hint or manual resize handle. Input grows and shrinks with its text, up to 360px or 42% of the viewport; longer drafts scroll inside the field. Existing messages replace the greeting. The composer remains anchored while the conversation scrolls independently. The plus disclosure opens real prompt suggestions; the assistant disclosure carries existing chat status. Context links lead to actual assignments and plan views. No inactive model selector, microphone or attachment controls are invented.

## Color and type

Ground #101010; rail #0d0e10; sidebar #141518; composer #1d1e22; separators #2b2d33. Text #e3e4e9, supporting text #a2a5af, links #a6bcff, action #3559cd, focus #91acff. The blue radial background appears only on the untouched empty working surface. Any text in the composer or an existing conversation switches the workspace to solid #101010, including while an answer is pending. It is a pinned reference effect, not a status signal.

Use Segoe UI/system sans, 14px base, 15px conversation, 16px input, and a fluid 28–40px empty heading. Technical identifiers remain Consolas. Buttons retain visible focus, pending and disabled states. Success, warning and error use dark semantic pairs, always with their existing labels.

## Capability and evidence boundary

Preserve all assignments, source links, completion marks, plan review, exact-version approval and separate apply actions, run journal, auth, CSRF and ownership boundaries. Keep unavailable Calendar writes explicit near the composer. Restyle tasks, plan and journal coherently without changing their data or execution behavior. Google sign-in keeps its official provider rendering and transparent frame fix. Existing dark authentication is retained.

Desktop and mobile checks must cover empty conversation, long replies with stable input position, navigation, prompt filling, visible focus and overflow. UI screenshots and synthetic chats do not prove real Calendar writes or cloud agent deployment.
