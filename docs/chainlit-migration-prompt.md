# Prompt: migrate the chat UI from Streamlit to Chainlit

Copy everything below the line into your coding agent as its task prompt.

---

## Objective

This project (`Agentic-Chatbot-Using-LangGraph`) has a Streamlit UI in `app.py`
that drives a LangGraph multi-agent chatbot defined in `backend.py`, with chat
history/titles/feedback/model-catalog persistence in `chat_store.py`. Build a
**new, parallel** Chainlit UI that replicates `app.py`'s behavior, without
touching the existing Streamlit app.

**Hard constraints:**

- Do **not** modify `backend.py` or `chat_store.py`'s existing public functions
  or signatures. They are the shared backend contract; both UIs call into them.
  You may *add* new functions to `chat_store.py` if the Chainlit data layer
  needs a capability that doesn't exist yet (e.g., a method that returns a
  single thread's full history keyed the way Chainlit wants it) — but don't
  change what's already there, since `app.py` depends on it.
- Do **not** modify or delete `app.py`. It keeps running exactly as it does
  today. This is an additive migration: `app.py` (Streamlit) and
  `chainlit_app.py` (Chainlit) both exist, both work, pointed at the same
  `backend.py` graph and the same `agentic_chatbot.db` / `chat_titles.json` /
  `chat_feedback.json` files.
- Do **not** touch the Dockerfile, CI/CD, or deployment docs. This is
  local-only for now; deployment wiring is a separate follow-up once the
  Chainlit UI is validated.
- New dependency: add `chainlit` to `requirements.txt`/`pyproject.toml`. Don't
  pin a version speculatively — install the latest stable release and record
  whatever version `pip show chainlit` reports.

**Before writing any Chainlit-specific code**, read the actual installed
package source for the APIs below rather than trusting this prompt's
signatures from memory — Chainlit's API (especially the data layer ABC) has
changed across versions, and getting a method signature wrong fails silently
or breaks the sidebar in ways that are hard to trace back to the cause. At
minimum, open:

- `chainlit/data/base.py` for the exact `BaseDataLayer` abstract method
  signatures (names, parameters, return types/TypedDicts) before implementing
  the custom data layer in step 3 below.
- `chainlit/element.py` and `chainlit/message.py` for `cl.Message`, `cl.Step`,
  `cl.File`/`cl.Image`/`cl.Pdf` element constructors.
- `chainlit/action.py` for `cl.Action` / `@cl.action_callback`.
- Search for `AskActionMessage` and `ChatSettings` classes for their current
  constructor arguments.
- The `chainlit init`-generated `.chainlit/config.toml` for exactly which keys
  exist in this installed version (e.g. the file-upload feature flag's exact
  name and location — it has moved between config sections in past releases).

If any Chainlit API described below doesn't exist in the installed version, or
exists with a different shape, use the closest current equivalent and note the
substitution in your summary at the end — don't silently skip the feature.

## Current system, in brief

`backend.py` exposes: `chatbot` (a compiled LangGraph `StateGraph` with a
`SqliteSaver` checkpointer against `agentic_chatbot.db`), `tools` (14 tools
across 4 specialist agents — Data_Agent, Research_Agent, Financial_Agent,
Memory_Agent — routed by a `supervisor` node, with a `synthesizer` node
producing the final answer on multi-step plans), `ingest_rag_document()`,
`chart_id_in()`/`load_chart()`, `extract_id_in()`/`load_extract()`, and model
state (`backend.llm`, `backend.set_model()`, `backend.current_model`).

The graph state includes a `plan` (list of `{agent, task, status, note}`) that
the supervisor streams updates to, and one tool — `purchase_stock` — calls
`interrupt(...)` to pause the graph for human approval, resumed via
`Command(resume="yes"|"no")`.

`chat_store.py` (bound once via `chat_store.bind(backend)`) is the persistence
and business-logic layer both UIs should call into: thread listing/summaries
(`thread_summaries()`, reading the LangGraph checkpoint log directly — there is
no separate threads table), title storage (`chat_titles.json`), feedback
storage (`chat_feedback.json`), history reconstruction from a LangGraph
checkpoint (`load_history(thread_id)`), interrupt detection
(`pending_interrupt(state)`), attachment/file-note helpers, and the model
catalog (`available_models()`, `use_model()`).

Full inventory of `app.py`'s behavior to replicate is in the "Feature map"
section below.

## Target file layout

```
chainlit_app.py            # new: Chainlit entry point (@cl.on_chat_start, @cl.on_message, etc.)
chainlit_data_layer.py     # new: custom BaseDataLayer subclass wrapping chat_store.py
.chainlit/config.toml      # new: chainlit init output, then edited for file upload + theme
.chainlit/translations/    # leave at chainlit init defaults unless asked
```

`chainlit_app.py` imports `backend` and `chat_store` exactly like `app.py`
does today (`import backend as backend`, `import chat_store`,
`chat_store.bind(backend)`).

## Feature map: Streamlit → Chainlit

Work through this table in order; each row is roughly one implementation unit.
Streamlit behavior descriptions are the ground truth for what "done" means —
re-read the corresponding section of `app.py` before implementing each row if
anything here is ambiguous.

| # | Streamlit behavior (`app.py`) | Chainlit approach |
|---|---|---|
| 1 | Sidebar thread list, newest-first, capped at 10 with "See more", search box, rename/delete popover | Chainlit renders its own thread sidebar **from the data layer** — you don't hand-build this UI. Implement `list_threads()` (paginated) in the custom data layer backed by `chat_store.thread_summaries()`; implement `delete_thread()` backed by `chat_store.delete_thread()`; implement `update_thread()` (used for rename) to call `chat_store.save_title()`. Chainlit's built-in sidebar search operates over whatever `list_threads()` returns — confirm in the data layer's actual signature whether it takes a search/filter param or whether search is client-side over the full list. |
| 2 | "New chat" button | Chainlit creates a new thread automatically per session; `@cl.on_chat_start` is where you initialize a fresh `thread_id` (reuse `app.py`'s `new_thread_id()` pattern: `str(uuid.uuid4())`) if no thread is being resumed. |
| 3 | Resuming an existing chat (clicking a sidebar row) | Implement `@cl.on_chat_resume` — Chainlit calls this with the thread being reopened. Rebuild in-memory state from `chat_store.load_history(thread_id)` the same way `app.py`'s `init_state()` does, and replay each historical message/tool-step back into the UI (Chainlit expects you to re-`send()` or reconstruct elements for resumed history — check current docs/source for the exact resume contract, it has changed across versions). |
| 4 | "Agent tools" sidebar list (badges) | Chainlit doesn't have a sidebar-badge-list primitive. Simplest faithful option: a `cl.ChatSettings` read-only section, or a fixed `cl.Message` sent once at `@cl.on_chat_start` listing available tools. Don't over-build this — a single informational element is enough. |
| 5 | Model picker (popover chip, grouped by provider, switches `backend.set_model`) | Use `cl.ChatSettings` with a `Select` widget (options built from `chat_store.available_models()`, grouped/labelled the same way `render_model_picker()` does) and `@cl.on_settings_update` to call `chat_store.use_model(id, provider)`. This shows as a settings-gear panel rather than an inline chip — that's the correct Chainlit idiom, don't fight it by trying to build a custom composer chip. |
| 6 | Message grouping by consecutive role | Chainlit already renders one bubble per `cl.Message.send()` call — you don't need manual grouping logic. Send one `cl.Message` per logical turn; tool calls become nested `cl.Step`s under that message rather than separate bubbles (see row 8). |
| 7 | Markdown-safe rendering (`markdown_safe()` escaping `$123`) | Port `markdown_safe()` from `app.py` unchanged (it's pure text transformation, no Streamlit dependency) and apply it to `content` before constructing each `cl.Message`. Verify whether Chainlit's markdown renderer has the same LaTeX-triggering behavior on bare `$...$` — if it doesn't, this step may be unnecessary; test before porting it, don't port defensively. |
| 8 | Tool-call status widgets (running → done, icon + label from `TOOLS_UI`/`tool_meta`) | Use `async with cl.Step(name=<label>, type="tool") as step:` around each tool call in the streaming loop; set `step.output` when done. Port the `TOOLS_UI` dict and `tool_meta()` function from `app.py` unchanged — they're pure data/logic, no Streamlit dependency. |
| 9 | Live plan display (multi-step plan from `updates` stream mode) | Render the plan as a `cl.Step` (or a dedicated `cl.Message` updated in place via `.update()`) that gets rewritten each time the `supervisor` node emits a `plan` delta — same trigger condition as `render_plan()` in `app.py` (skip if `len(plan) <= 1`). |
| 10 | `filter_think()` — strips `<think>` blocks, mutes non-synthesizer prose on multi-step plans, drives tool-status widgets | Port the core generator logic from `app.py` almost unchanged; only the *sink* changes (it currently writes into Streamlit containers — repoint those writes at `cl.Step`/`cl.Message.stream_token()`). This is the most logic-dense piece of `app.py` — read it fully (`filter_think` and its caller) before touching it, and preserve its comments; they document real edge cases (mid-turn handoffs, multi-provider content-block streaming). |
| 11 | Token streaming into the answer bubble (`st.write_stream`) | `msg = cl.Message(content=""); await msg.send()`, then `await msg.stream_token(token)` per chunk from the filtered stream, `await msg.update()` at the end. |
| 12 | Image/attachment thumbnails, PDF ingestion, spreadsheet uploads | Enable file uploads in `.chainlit/config.toml` (find the current key for spontaneous file upload + accepted MIME/extension list in the installed version's config schema) so `message.elements` is populated on `@cl.on_message`. For each element: if it's a PDF, save to a temp path and call `backend.ingest_rag_document()` exactly as `app.py` does; if image/spreadsheet, save via a ported version of `save_upload()` into `uploads/`. Render received images back using `cl.Image(path=..., display="inline")` attached to the user's echoed message. |
| 13 | Human-in-the-loop purchase approval (`Approve`/`Decline` buttons, `Command(resume=...)`) | Use `cl.AskActionMessage` with two actions (values `"yes"`/`"no"`, matching the literal strings `backend.py`'s `purchase_stock` checks for) to block for the user's choice, then resume the graph with `Command(resume=response["value"])` exactly as `app.py` does. Detect the interrupted state the same way: `chatbot.get_state(config)` + `chat_store.pending_interrupt(state)`. |
| 14 | Copy button | Skip building this — Chainlit messages have built-in copy-to-clipboard in the UI already. |
| 15 | Regenerate button | Add a `cl.Action` (e.g. `name="regenerate"`) attached to each assistant message; `@cl.action_callback("regenerate")` re-runs the turn using the previous user message, same lookup logic as `queue_prompt`/the regen handler in `app.py`. |
| 16 | Thumbs feedback (`st.feedback`, persisted via `chat_store.save_feedback`) | Wire the custom data layer's `upsert_feedback()` (and `delete_feedback()` if it exists as a separate method in the installed version) to call `chat_store.save_feedback()`. Chainlit renders its own thumbs UI on every message automatically once a data layer is registered — you don't build this widget by hand. |
| 17 | Toasts on rename/delete | Chainlit doesn't have a direct toast primitive tied to sidebar actions the way Streamlit's `st.toast` does, since rename/delete happen through the data layer, not a UI callback you control. Skip trying to replicate a toast for these; if Chainlit's own UI gives feedback on delete/rename (many client versions do via the sidebar itself), that's sufficient. |
| 18 | Chart rendering from tool output (`backend.chart_id_in`/`load_chart`) | Render inside the relevant `cl.Step`'s output as an image (render the chart spec to a PNG/HTML the way `render_tool_result` does today, or check whether Chainlit has a native chart element in the installed version) rather than Streamlit's `st.line_chart`. |
| 19 | DataFrame extract rendering + CSV download (`backend.extract_id_in`/`load_extract`) | Render as a Markdown table (or `cl.File` for the CSV download) inside the step output — check `cl.File`'s constructor for whether it takes bytes/path. |
| 20 | Suggestion pills on empty state | A small set of `cl.Action`s shown in the first `@cl.on_chat_start` message, or Chainlit's `starters` feature (`@cl.set_starters` returning `cl.Starter` objects) if the installed version has it — that's the more idiomatic fit, prefer it if available. |
| 21 | Error handling (turn failure appended to history as a bubble, not `st.error`) | Catch exceptions around the stream loop and `await cl.Message(content=f"**The turn failed.** {err}").send()`, matching `app.py`'s current error copy. |
| 22 | Empty-model-output placeholder | Port the same placeholder text/condition unchanged. |

## Custom data layer (`chainlit_data_layer.py`)

This is the piece that makes "keep `chat_store.py`, get Chainlit's native
sidebar" work. Implement a class extending Chainlit's `BaseDataLayer` (exact
class/module path confirmed from `chainlit/data/base.py` per the note above),
where every method delegates to an existing or newly-added `chat_store.py`
function rather than touching SQLite/JSON directly. Concretely, at minimum you
will need to implement (adjust names to match the installed ABC):

- Thread listing → `chat_store.thread_summaries()`
- Thread deletion → `chat_store.delete_thread()`
- Thread rename/update → `chat_store.save_title()`
- Feedback upsert → `chat_store.save_feedback()`
- User/session identity — this app has no auth; use a single fixed
  pseudo-user (matching how `app.py` has no login concept today) unless the
  installed `BaseDataLayer` requires more.
- Step/element persistence — LangGraph's own `SqliteSaver` checkpoint already
  captures message history (`chat_store.load_history()` proves this is
  sufficient for `app.py`'s history rebuild), so these methods can likely be
  no-ops or thin passthroughs rather than a second persistence path. Confirm
  this assumption against how Chainlit actually calls these methods before
  deciding to no-op them — if the sidebar preview text or resume flow turns
  out to depend on step data specifically, wire it to `chat_store` instead of
  no-opping.

Register the data layer the way the installed Chainlit version expects
(commonly a `@cl.data_layer` decorated factory function returning an instance)
in `chainlit_app.py`.

## Testing checklist

Run `chainlit run chainlit_app.py` and manually verify, mirroring the same
pass done for the Streamlit UI:

- New chat → send a message with no tools needed → plain streamed answer.
- A message that triggers exactly one tool (e.g. "what's the weather in
  Lahore") → step widget shows running → done with correct icon/label.
- A message that triggers the multi-step supervisor plan (e.g. one of the
  existing "Comprehensive System Diagnostic Test" style prompts already saved
  in chat history) → plan renders and updates live, only the synthesizer's
  prose appears once.
- Upload a PDF → confirm it's ingested (`ingest_rag_document`) and a
  follow-up question can retrieve from it.
- Upload an image → confirm it round-trips through `uploads/` and renders.
- Trigger `purchase_stock` → confirm the Approve/Decline prompt blocks
  correctly and both the "yes" and "no" paths resume the graph correctly.
- Rename a thread from the sidebar → confirm `chat_titles.json` is updated and
  survives a reload.
- Delete a thread from the sidebar → confirm it disappears from both the
  Chainlit sidebar and a subsequent `chat_store.thread_summaries()` call.
- Give a message thumbs up/down → confirm `chat_feedback.json` is updated.
- Switch models mid-conversation via `cl.ChatSettings` → confirm the next
  turn uses the newly selected model (check `backend.current_model`).
- Restart the Chainlit process and resume an old thread from the sidebar →
  confirm history renders correctly, including past tool-step outputs.
- Confirm `app.py` (Streamlit) still runs unmodified and independently against
  the same `agentic_chatbot.db`.

## Deliverable

`chainlit_app.py`, `chainlit_data_layer.py`, an updated `.chainlit/config.toml`,
and the new `chainlit` dependency added to `requirements.txt`/`pyproject.toml`.
At the end, report: which Chainlit APIs referenced in this prompt didn't match
the installed version and what you substituted, and which rows in the feature
map (if any) you couldn't replicate and why.
