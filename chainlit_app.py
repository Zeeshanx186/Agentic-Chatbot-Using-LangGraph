"""Chainlit UI for the Agentic Chatbot — parallel to app.py (Streamlit).

Both UIs share the same backend.py graph, chat_store.py persistence, and
agentic_chatbot.db / chat_titles.json / chat_feedback.json files.

Run with:  chainlit run chainlit_app.py
"""

import backend as backend
from backend import chatbot, ingest_rag_document, tools
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.types import Command

import chat_store

chat_store.bind(backend)

import json
import os
import re
import tempfile
import uuid

import chainlit as cl
from chainlit.input_widget import Select

from chainlit_data_layer import ChatStoreDataLayer, _FIXED_USER_IDENTIFIER


# ============================== Data layer ==============================

@cl.data_layer
def get_data_layer():
    return ChatStoreDataLayer()


# ============================== Auth ==============================
# The /project/threads endpoint that backs the sidebar's thread list 401s
# whenever there is no authenticated user, regardless of whether a data layer
# is registered. This app has no real accounts, so header_auth_callback is
# used purely to satisfy that check: it runs silently on load (no login page
# is shown, unlike password/OAuth auth) and always authenticates the same
# fixed local user that ChatStoreDataLayer already assumes everywhere.
@cl.header_auth_callback
async def header_auth(headers) -> cl.User:
    return cl.User(identifier=_FIXED_USER_IDENTIFIER)


# ============================== Ported from app.py ==============================

# Tool metadata — pure data, no Streamlit dependency.
TOOLS_UI = {
    "search":        ("🔍", "Searching the web", "Searched the web"),
    "fetch_url":     ("🔗", "Reading the page", "Read the page"),
    "extract_text":  ("📄", "Reading the image", "Read the image"),
    "datetime":      ("🕐", "Checking the date", "Checked the date"),
    "weather":       ("⛅", "Checking the weather", "Checked the weather"),
    "stock_history": ("📈", "Pulling price history", "Pulled price history"),
    "stock_price":   ("📊", "Looking up the stock price", "Looked up the stock price"),
    "purchase":      ("🛒", "Placing the order", "Placed the order"),
    "retriever":     ("📑", "Searching your documents", "Searched your documents"),
    "wikipedia":     ("📚", "Reading Wikipedia", "Read Wikipedia"),
    "inspect":       ("🗃️", "Inspecting the file", "Inspected the file"),
    "pandas":        ("⚙️", "Extracting spreadsheet data", "Extracted spreadsheet data"),
    "plot":          ("📊", "Drawing the chart", "Drew the chart"),
    "remember":      ("🔖", "Saving to memory", "Saved to memory"),
    "recall":        ("🧠", "Checking memory", "Checked memory"),
    "forget":        ("🗑️", "Clearing memory", "Cleared memory"),
}


def tool_meta(tool_name):
    """Map a raw tool name to (running label, done label, icon emoji)."""
    name = (tool_name or "tool").lower()
    for key, (icon, running, done) in TOOLS_UI.items():
        if key in name:
            return running, done, icon
    return f"Running {tool_name}", f"Used {tool_name}", "🔧"


# Streamlit's markdown reads `$...$` as LaTeX. Chainlit has latex=false in
# config, so this escaping should be unnecessary, but kept defensively.
_CURRENCY_DOLLAR = re.compile(r"(?<!\$)\$(?=\s?\d)")


def markdown_safe(text):
    """Escape currency dollar signs so they survive markdown as text.

    Fenced code is skipped: a backslash inserted into a code block would be
    shown literally rather than swallowed, which is the same bug in reverse.
    Splitting on the fence puts code in the odd-numbered segments.
    """
    segments = str(text).split("```")
    for index in range(0, len(segments), 2):
        segments[index] = _CURRENCY_DOLLAR.sub(lambda _: "\\$", segments[index])
    return "```".join(segments)


SUGGESTIONS = {
    "🔍 AI news this week":
        "What are the most important AI news stories from this week?",
    "🔢 Crunch a number":
        "What is the compound interest on 25000 at 7.5% for 12 years?",
    "⛅ Weather in Lahore":
        "What is the current weather in Lahore?",
    "📊 Price of NVDA":
        "What is the current stock price of NVDA?",
}

# Nodes whose tokens are meant for the user.
SPEAKING_NODES = {"Data_Agent", "Research_Agent", "Financial_Agent",
                  "Memory_Agent", "synthesizer"}

PLAN_ICONS = {
    "pending": "⬜",
    "running": "🔄",
    "done": "✅",
    "failed": "❌",
    "blocked": "❓",
}

UPLOAD_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "uploads")
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg"}


def save_upload(file_name, file_bytes):
    """Persist an uploaded file and return its absolute path."""
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", file_name)
    path = os.path.join(UPLOAD_DIR, safe_name)

    stem, extension = os.path.splitext(path)
    counter = 1
    while os.path.exists(path):
        path = "%s_%d%s" % (stem, counter, extension)
        counter += 1

    with open(path, "wb") as f:
        f.write(file_bytes)
    return path


def render_tool_result_text(content):
    """Render a tool result as text for a cl.Step output.

    Charts and extracts are rendered differently than in Streamlit because
    Chainlit doesn't have native chart/dataframe widgets. Charts are noted
    textually, extracts are rendered as markdown tables with CSV download.
    """
    # Chart rendering
    chart_id = backend.chart_id_in(content)
    if chart_id:
        spec = backend.load_chart(chart_id)
        if spec:
            return f"📊 Chart drawn: {spec.get('kind', 'chart')} of {', '.join(spec.get('y', []))} vs {spec.get('x', '?')}"
        return "This chart is no longer on disk."

    # Extract rendering
    extract_id = backend.extract_id_in(content)
    if extract_id:
        df = backend.load_extract(extract_id)
        if df is not None:
            preview = df.head(20).to_markdown(index=False)
            return f"**{df.shape[0]} rows × {df.shape[1]} columns**\n\n{preview}"
        return "This extraction is no longer on disk."

    # Try JSON
    try:
        parsed = json.loads(content)
    except Exception:
        parsed = None

    if isinstance(parsed, (dict, list)):
        return "```json\n" + json.dumps(parsed, indent=2) + "\n```"

    return "```\n" + str(content) + "\n```"


# ============================== Model setup ==============================

def get_models():
    """Load the model catalog, cached for the session."""
    return chat_store.available_models()


def build_model_settings(models, current_id):
    """Build a ChatSettings Select widget from the model catalog."""
    items = {}
    for m in models:
        label = f"{m['label']}  ({m['note']})"
        items[label] = m["id"]

    return cl.ChatSettings([
        Select(
            id="model",
            label="Model",
            items=items,
            initial_value=current_id,
            description="Select the AI model to use",
        ),
    ])


# ============================== Starters ==============================

@cl.set_starters
async def set_starters():
    return [
        cl.Starter(
            label="🔍 AI news this week",
            message="What are the most important AI news stories from this week?",
            icon="/public/favicon.svg",
        ),
        cl.Starter(
            label="🔢 Crunch a number",
            message="What is the compound interest on 25000 at 7.5% for 12 years?",
            icon="/public/favicon.svg",
        ),
        cl.Starter(
            label="⛅ Weather in Lahore",
            message="What is the current weather in Lahore?",
            icon="/public/favicon.svg",
        ),
        cl.Starter(
            label="📊 Price of NVDA",
            message="What is the current stock price of NVDA?",
            icon="/public/favicon.svg",
        ),
    ]


# ============================== Chat lifecycle ==============================

@cl.on_chat_start
async def on_chat_start():
    """Initialize a new chat session."""
    thread_id = str(uuid.uuid4())
    cl.user_session.set("thread_id", thread_id)
    cl.user_session.set("history", [])

    # Load and apply model
    models = get_models()
    cl.user_session.set("models", models)

    known = [m["id"] for m in models]
    current = chat_store.active_model()
    model_id = current if current in known else known[0]
    cl.user_session.set("model_id", model_id)

    # Apply the model to the backend
    selected = next((m for m in models if m["id"] == model_id), models[0])
    chat_store.use_model(selected["id"], selected["provider"])

    # Show model settings. Deliberately no cl.Message here: sending one at
    # chat-start ends the welcome/starters screen immediately (Chainlit treats
    # any message as "the conversation has begun"), which is why an earlier
    # version of this handler caused a flash straight from the logo screen to
    # a bare "Agent tools" bubble with the starters gone. The tool list lives
    # in chainlit.md (the "Readme" link) instead.
    settings = build_model_settings(models, model_id)
    await settings.send()


@cl.on_chat_resume
async def on_chat_resume(thread: dict):
    """Resume a previously saved chat thread."""
    thread_id = thread["id"]
    cl.user_session.set("thread_id", thread_id)

    # Load and apply model
    models = get_models()
    cl.user_session.set("models", models)

    known = [m["id"] for m in models]
    current = chat_store.active_model()
    model_id = current if current in known else known[0]
    cl.user_session.set("model_id", model_id)

    selected = next((m for m in models if m["id"] == model_id), models[0])
    chat_store.use_model(selected["id"], selected["provider"])

    settings = build_model_settings(models, model_id)
    await settings.send()

    # Replay history from the LangGraph checkpoint
    history = chat_store.load_history(thread_id)
    cl.user_session.set("history", history)

    for msg in history:
        role = msg["role"]
        kind = msg.get("type", "text")
        content = msg.get("content", "")

        if role == "user":
            if kind == "image":
                # Image attachment: show as inline image if file still exists
                if os.path.exists(content):
                    elements = [cl.Image(name="attachment", path=content, display="inline")]
                    await cl.Message(content="", author="User", elements=elements).send()
            elif kind == "text":
                await cl.Message(content=content, author="User", type="user_message").send()

        elif role == "assistant":
            if kind == "tool":
                tool_name = msg.get("tool_name", "tool")
                _, done_label, icon = tool_meta(tool_name)
                tool_output = render_tool_result_text(content)

                # Create a step for the tool
                step = cl.Step(name=f"{icon} {done_label}", type="tool")
                step.output = tool_output
                await step.send()

            elif kind == "text":
                safe_content = markdown_safe(content)
                await cl.Message(content=safe_content).send()


# ============================== Settings ==============================

@cl.on_settings_update
async def on_settings_update(settings):
    """Handle model switch from ChatSettings."""
    model_id = settings.get("model")
    if not model_id:
        return

    models = cl.user_session.get("models") or get_models()
    selected = next((m for m in models if m["id"] == model_id), None)
    if selected:
        chat_store.use_model(selected["id"], selected["provider"])
        cl.user_session.set("model_id", model_id)
        await cl.Message(
            content=f"Switched to **{selected['label']}** ({selected['note']})",
            author="System",
        ).send()


# ============================== Regenerate ==============================

@cl.action_callback("regenerate")
async def on_regenerate(action: cl.Action):
    """Re-run the previous user message."""
    previous_prompt = action.payload.get("prompt", "")
    if previous_prompt:
        # Create a synthetic message and process it
        message = cl.Message(content=previous_prompt, author="User", type="user_message")
        await process_message(message)


# ============================== Message handling ==============================

@cl.on_message
async def on_message(message: cl.Message):
    """Handle incoming user messages and file uploads."""
    await process_message(message)


async def process_message(message: cl.Message):
    """Core message processing — handles text, files, and streaming."""
    thread_id = cl.user_session.get("thread_id")
    if not thread_id:
        thread_id = str(uuid.uuid4())
        cl.user_session.set("thread_id", thread_id)

    history = cl.user_session.get("history") or []
    had_user_message = any(m["role"] == "user" for m in history)

    user_input = message.content or ""
    display_input = user_input

    attached_paths = []
    attached_images = []
    indexed_names = []

    # ---- Handle file uploads ----
    if message.elements:
        for element in message.elements:
            if not hasattr(element, "path") or not element.path:
                continue

            file_path = element.path
            file_name = element.name or os.path.basename(file_path)
            extension = os.path.splitext(file_name)[1].lower()

            if extension == ".pdf":
                try:
                    ingest_rag_document(file_path, file_name)
                    indexed_names.append(file_name)
                    await cl.Message(
                        content=f"📑 **{file_name}** — added to the knowledge base",
                        author="System",
                    ).send()
                except Exception as e:
                    await cl.Message(
                        content=f"❌ **{file_name}** — could not be indexed: {e}",
                        author="System",
                    ).send()
            else:
                try:
                    with open(file_path, "rb") as f:
                        file_bytes = f.read()
                    saved_path = save_upload(file_name, file_bytes)
                    attached_paths.append(saved_path)
                    if extension in IMAGE_EXTENSIONS:
                        attached_images.append(saved_path)
                    await cl.Message(
                        content=f"📎 **{file_name}** — saved",
                        author="System",
                    ).send()
                except Exception as e:
                    await cl.Message(
                        content=f"❌ **{file_name}** — could not be saved: {e}",
                        author="System",
                    ).send()

    # ---- Build the agent-facing input with file notes ----
    turn_notes = []

    if indexed_names:
        turn_notes.append(chat_store.indexed_note(indexed_names))

    if attached_paths:
        listing = "\n".join("- " + path for path in attached_paths)
        sheets = [p for p in attached_paths if p not in attached_images]
        kinds = []
        if attached_images:
            kinds.append("read the text out of every image")
        if sheets:
            kinds.append("inspect the structure of every spreadsheet")

        turn_notes.append((
            "%d file(s) are ALREADY SAVED on disk at the exact paths below and are "
            "available now. Never ask the user to upload them again. The job is to "
            "%s.\n%s"
        ) % (len(attached_paths), " and ".join(kinds), listing))

    if turn_notes:
        note = " ".join(turn_notes)
        if user_input:
            user_input = note + "\n\n" + user_input
        else:
            user_input = note + "\n\nSummarise what each file contains."
            display_input = "Attached " + ", ".join(
                [os.path.basename(p) for p in attached_paths] + indexed_names
            )

    if not user_input:
        return

    # ---- Record in history ----
    for image_path in attached_images:
        history.append({"role": "user", "type": "image", "content": image_path})
    history.append({"role": "user", "type": "text", "content": display_input})

    # ---- Check for pending interrupt (HITL purchase approval) ----
    config = {
        "configurable": {"thread_id": thread_id},
        "metadata": {"thread_id": thread_id},
        "run_name": "chat_trace",
    }

    state_snapshot = chatbot.get_state(config)
    interrupt_text = chat_store.pending_interrupt(state_snapshot)
    is_interrupted = interrupt_text is not None

    if is_interrupted:
        # Show the interrupt prompt and ask for approval
        res = await cl.AskActionMessage(
            content=interrupt_text or "The agent needs your approval to continue.",
            actions=[
                cl.Action(name="approve", payload={"value": "yes"}, label="✅ Approve"),
                cl.Action(name="decline", payload={"value": "no"}, label="❌ Decline"),
            ],
            timeout=300,
        ).send()

        if res:
            resume_value = res.get("payload", {}).get("value", "no")
        else:
            resume_value = "no"

        decision = "Approved." if resume_value == "yes" else "Declined."
        history.append({"role": "user", "type": "text", "content": decision})

        stream_input = Command(resume=resume_value)
    else:
        stream_input = {"messages": [HumanMessage(content=user_input)]}

    # ---- Title generation for first message ----
    is_first_message = not had_user_message

    if is_first_message:
        title = (
            chat_store.title_from_attachments(attached_paths)
            or chat_store.fallback_title(display_input)
        )
        chat_store.save_title(thread_id, title)

    # ---- Stream the response ----
    answer_msg = cl.Message(content="")
    await answer_msg.send()

    plan_msg = None  # Lazy-created for multi-step plans
    in_think = False
    buffer = ""
    current_node = None
    spoke = False
    multi_step = False
    full_answer = ""
    current_step = None

    try:
        stream = chatbot.stream(
            stream_input,
            config=config,
            stream_mode=["messages", "updates"],
        )

        for mode, payload in stream:
            if mode == "updates":
                for node_name, delta in (payload or {}).items():
                    if node_name == "supervisor" and delta and "plan" in delta:
                        plan = delta["plan"]
                        if plan and len(plan) > 1:
                            multi_step = True
                            # Render the plan
                            plan_text = "**📋 Plan**\n\n"
                            for i, step in enumerate(plan, 1):
                                icon = PLAN_ICONS.get(step.get("status", "pending"), PLAN_ICONS["pending"])
                                plan_text += f"{icon}  **{i}. {step.get('agent', '?')}** — {step.get('task', '')}\n"

                            if plan_msg is None:
                                plan_msg = cl.Message(content=plan_text, author="System")
                                await plan_msg.send()
                            else:
                                plan_msg.content = plan_text
                                await plan_msg.update()
                continue

            message_chunk, metadata = payload
            node = (metadata or {}).get("langgraph_node")

            # Node handoff
            if node and node != current_node:
                if buffer and not in_think:
                    await answer_msg.stream_token(buffer)
                    full_answer += buffer
                    spoke = True
                buffer, in_think = "", False
                if current_node is not None and spoke:
                    await answer_msg.stream_token("\n\n")
                    full_answer += "\n\n"
                current_node = node
                spoke = False

            # Tool call chunks — show as steps
            if (isinstance(message_chunk, AIMessage)
                    and getattr(message_chunk, "tool_call_chunks", None)):
                for tc in message_chunk.tool_call_chunks:
                    if tc.get("name"):
                        running, _, icon = tool_meta(tc["name"])
                        if current_step is not None:
                            # Close previous step
                            pass
                        current_step = cl.Step(name=f"{icon} {running}", type="tool")
                        await current_step.__aenter__()

            elif isinstance(message_chunk, ToolMessage):
                tool_name = getattr(message_chunk, "name", "tool")
                _, done, icon = tool_meta(tool_name)

                tool_output = render_tool_result_text(message_chunk.content)

                if current_step is not None:
                    current_step.name = f"{icon} {done}"
                    current_step.output = tool_output
                    await current_step.__aexit__(None, None, None)
                    current_step = None
                else:
                    step = cl.Step(name=f"{icon} {done}", type="tool")
                    step.output = tool_output
                    await step.send()

                # Extract rendering: offer CSV download
                extract_id = backend.extract_id_in(message_chunk.content)
                if extract_id:
                    df = backend.load_extract(extract_id)
                    if df is not None:
                        csv_bytes = df.to_csv(index=False).encode("utf-8")
                        csv_file = cl.File(
                            name=f"extract_{extract_id}.csv",
                            content=csv_bytes,
                            display="side",
                        )
                        await cl.Message(
                            content="📥 Download the extracted data:",
                            elements=[csv_file],
                        ).send()

                history.append({
                    "role": "assistant",
                    "type": "tool",
                    "tool_name": tool_name,
                    "content": message_chunk.content,
                })

            # AI text streaming
            if isinstance(message_chunk, AIMessage) and message_chunk.content:
                speakers = {"synthesizer"} if multi_step else SPEAKING_NODES
                if node is not None and node not in speakers:
                    continue

                content = message_chunk.content
                if isinstance(content, list):
                    content = "".join(
                        p.get("text", "") for p in content if isinstance(p, dict)
                    )
                if not content:
                    continue

                buffer += content
                while True:
                    if not in_think:
                        start_idx = buffer.find("<think>")
                        if start_idx != -1:
                            if start_idx > 0:
                                chunk = buffer[:start_idx]
                                await answer_msg.stream_token(chunk)
                                full_answer += chunk
                                spoke = True
                            buffer = buffer[start_idx + len("<think>"):]
                            in_think = True
                        else:
                            if len(buffer) > 6:
                                chunk = buffer[:-6]
                                await answer_msg.stream_token(chunk)
                                full_answer += chunk
                                spoke = True
                                buffer = buffer[-6:]
                            break
                    else:
                        end_idx = buffer.find("</think>")
                        if end_idx != -1:
                            buffer = buffer[end_idx + len("</think>"):]
                            buffer = buffer.lstrip("\n")
                            in_think = False
                        else:
                            if len(buffer) > 7:
                                buffer = buffer[-7:]
                            break

        # Flush remaining buffer
        if not in_think and buffer:
            await answer_msg.stream_token(buffer)
            full_answer += buffer

    except Exception as e:
        error_text = f"❌ **The turn failed.** {e}"
        await answer_msg.stream_token(error_text)
        full_answer = error_text
        history.append({
            "role": "assistant",
            "type": "text",
            "content": error_text,
        })

    # Finalize the answer message. The regenerate action is attached to this
    # same message (not sent as a separate trailing message) so the turn
    # renders one avatar, not two.
    answer_msg.content = markdown_safe(full_answer)

    if full_answer.strip():
        history.append({"role": "assistant", "type": "text", "content": full_answer})
        answer_msg.actions = [
            cl.Action(
                name="regenerate",
                payload={"prompt": display_input},
                label="🔄 Regenerate",
                tooltip="Ask again with the same prompt",
            ),
        ]

    await answer_msg.update()

    if not full_answer.strip() and not any(
            m.get("type") == "text" and m["role"] == "assistant"
            for m in history[-2:]):
        placeholder = (
            "ℹ️ The model returned no text this turn — it spent "
            "the reply on reasoning. Ask again, or pick another model."
        )
        history.append({"role": "assistant", "type": "text", "content": placeholder})
        answer_msg.content = placeholder
        await answer_msg.update()

    # ---- Post-turn title (real LLM title for first message) ----
    if is_first_message and full_answer.strip():
        title = (
            chat_store.title_from_attachments(attached_paths)
            or chat_store.generate_title(display_input)
        )
        chat_store.save_title(thread_id, title)

    # Check for new interrupt after the turn
    state_snapshot = chatbot.get_state(config)
    interrupt_text = chat_store.pending_interrupt(state_snapshot)
    if interrupt_text is not None:
        res = await cl.AskActionMessage(
            content=interrupt_text or "The agent needs your approval to continue.",
            actions=[
                cl.Action(name="approve", payload={"value": "yes"}, label="✅ Approve"),
                cl.Action(name="decline", payload={"value": "no"}, label="❌ Decline"),
            ],
            timeout=300,
        ).send()

        if res:
            resume_value = res.get("payload", {}).get("value", "no")
        else:
            resume_value = "no"

        decision = "Approved." if resume_value == "yes" else "Declined."
        history.append({"role": "user", "type": "text", "content": decision})

        # Resume the graph
        resume_msg = cl.Message(content="")
        await resume_msg.send()

        try:
            resume_stream = chatbot.stream(
                Command(resume=resume_value),
                config=config,
                stream_mode=["messages", "updates"],
            )

            resume_answer = ""
            for mode, rpayload in resume_stream:
                if mode == "updates":
                    continue

                rmsg_chunk, rmeta = rpayload
                if isinstance(rmsg_chunk, AIMessage) and rmsg_chunk.content:
                    rnode = (rmeta or {}).get("langgraph_node")
                    if rnode and rnode not in SPEAKING_NODES:
                        continue

                    rcontent = rmsg_chunk.content
                    if isinstance(rcontent, list):
                        rcontent = "".join(
                            p.get("text", "") for p in rcontent if isinstance(p, dict)
                        )

                    # Strip think tags simply
                    rcontent = chat_store.strip_think(rcontent)
                    if rcontent:
                        await resume_msg.stream_token(rcontent)
                        resume_answer += rcontent

                elif isinstance(rmsg_chunk, ToolMessage):
                    rtool_name = getattr(rmsg_chunk, "name", "tool")
                    _, rdone, ricon = tool_meta(rtool_name)
                    step = cl.Step(name=f"{ricon} {rdone}", type="tool")
                    step.output = render_tool_result_text(rmsg_chunk.content)
                    await step.send()

            if resume_answer:
                resume_msg.content = markdown_safe(resume_answer)
                await resume_msg.update()
                history.append({"role": "assistant", "type": "text", "content": resume_answer})

        except Exception as e:
            resume_msg.content = f"❌ **The turn failed.** {e}"
            await resume_msg.update()

    cl.user_session.set("history", history)
