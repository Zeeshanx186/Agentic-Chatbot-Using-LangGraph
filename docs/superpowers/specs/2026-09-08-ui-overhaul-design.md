# UI overhaul design

Date: 2026-09-08
Scope: `app.py` and a new `.streamlit/config.toml` only. No changes to `backend.py`,
`chat_store.py`, agent/tool logic, or any deployment file. Front-end appearance and
interaction polish only.

## Context

The app has no `.streamlit/config.toml`. It runs on stock Streamlit theme defaults;
every color currently visible comes from hand-rolled `color-mix(in srgb, currentColor
N%, transparent)` CSS in `app.py`'s `st.html` block, because that CSS was written
under the (now outdated for Streamlit 1.62) assumption that theme values aren't
available as CSS custom properties at runtime. Introducing a real theme is the
highest-leverage change available.

## Goals

1. **Theme system** — add `.streamlit/config.toml` with `[theme.light]` and
   `[theme.dark]` sections so users get Streamlit's native light/dark toggle (requires
   both sections defined; a single `[theme]` locks one mode).
   - Palette: near-black / near-white backgrounds, one accent color (indigo/violet)
     reused for `primaryColor` and sidebar `primaryColor`.
   - Fonts: `Inter` for `font`/`headingFont`, `JetBrains Mono` for `codeFont`, loaded
     via Google Fonts URL syntax.
   - `baseRadius` tuned to match the existing 8-10px radii already used in the custom
     CSS, so native widgets (buttons, inputs, popovers) match the hand-styled ones.
   - `chartCategoricalColors` set for future chart use, not currently exercised by
     any chart in the app.
2. **Visual polish** — revise the existing `st.html` CSS block to:
   - Give chat bubbles real spacing/shadow instead of the bare `st.chat_message`
     default.
   - Refine the empty-state hero (icon/title/caption block) and suggestion pills.
   - Add a subtle glass/blur treatment to the composer bar.
   - Theme scrollbars app-wide (currently only the model popover has a themed
     scrollbar).
   - Add visible focus rings for keyboard navigation.
   - Replace instant hover/opacity swaps with real `transition` on background,
     border, and opacity.
3. **Motion** — restrained, not flashy:
   - Fade/slide-in on new message groups.
   - Replace the plain thinking/status treatment with a subtly animated indicator.
   - Micro feedback (brief scale/opacity pulse) on button press and on copy.
   - Sidebar row hover gets a slight lift instead of a flat background swap.
4. **Responsiveness** — the model-picker chip
   (`[class*="st-key-modelpicker_container"]`) is pinned with fixed pixel offsets
   (`bottom: 64px; margin-left: 46px`) that assume desktop-width chat input. Add a
   narrow-viewport media query so the chip and composer don't overlap or clip on
   mobile widths. Verify the sidebar and chat list remain usable at narrow widths
   (Streamlit's own responsive sidebar collapse already covers the coarse case).
5. **Small UI-only interaction wins**:
   - `st.toast` confirmation on copy, save-title, and delete-chat, replacing the
     current silent `st.rerun()`.
   - Smooth scroll-to-latest-message behavior after a new message is appended.

## Explicitly out of scope

- Command palette / keyboard-shortcut framework — no native Streamlit primitive for
  this; would require a custom component (CCv2), which is more than "UI polish."
- Message editing/resend — touches chat history mutation and would need
  `backend.py`/graph replay logic, not pure UI.
- Any change to `TOOLS_UI`, tool behavior, model list, or `chat_store.py` persistence
  format.
- Any change to Dockerfile, CI/CD, or deployment docs.

## Approach notes

- Per the `developing-with-streamlit` skill, colors/fonts/radii go in
  `.streamlit/config.toml`, not CSS — matches the existing codebase's own stated
  intent (the comment at `app.py:44-46` already says colors should come from
  config.toml). The custom `st.html` CSS block stays reserved for layout/behavior
  that has no native Streamlit equivalent (row-as-button truncation, popover
  positioning, fixed chip placement), consistent with how the file already works.
- `st.context.theme.type` is available if any conditional (dark vs light) logic is
  needed in Python; expected to be unnecessary since config.toml handles both modes
  declaratively.
- No new Python dependencies.

## Testing

- Manual: run the app locally (`streamlit run app.py`), check both light and dark
  mode via the Streamlit app-menu toggle, resize to mobile width, exercise copy/
  save/delete toasts, send a message and confirm scroll + motion.
- No automated UI tests exist in this project; none are being added (out of scope).
