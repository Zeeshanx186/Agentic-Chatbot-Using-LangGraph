# pyrefly: ignore [missing-import]
import backend as backend
from backend import chatbot, ingest_rag_document, tools
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.types import Command

# pyrefly: ignore [missing-import]
import chat_store

chat_store.bind(backend)

import json
import os
import streamlit as st
import tempfile
import uuid

st.set_page_config(
    page_title="Agentic chatbot",
    page_icon=":material/smart_toy:",
    layout="centered",
    initial_sidebar_state="expanded",
)

# Only for things with no native equivalent: the sidebar conversation rows need
# to look like flat, truncating list items rather than buttons. Colors, fonts
# and radii all come from .streamlit/config.toml.
st.html("""
<style>
    /* Tight, ChatGPT-style conversation list. */
    [class*="st-key-chatlist"] {
        gap: 2px;
    }
    [class*="st-key-chatrow_"] {
        border-radius: 8px;
        padding-right: 2px;
    }
    [class*="st-key-chatrow_"]:hover {
        background: color-mix(in srgb, currentColor 8%, transparent);
    }
    [class*="st-key-chatrow_active_"] {
        background: color-mix(in srgb, currentColor 12%, transparent);
    }
    [class*="st-key-chatrow_"] .stButton > button {
        background: transparent;
        border: none;
        box-shadow: none;
        border-radius: 8px;
        font-weight: 400;
        min-height: 34px;
        padding: 4px 10px;
    }
    /* The button's inner flex wrapper centres its label; force it left and let
       the markdown span take the full width so the ellipsis has room. */
    [class*="st-key-chatrow_"] .stButton > button > div,
    [class*="st-key-chatrow_"] .stButton > button > div > span {
        justify-content: flex-start;
        flex: 1 1 auto;
        min-width: 0;
        width: 100%;
    }
    [class*="st-key-chatrow_"] .stButton > button [data-testid="stMarkdownContainer"] {
        width: 100%;
        min-width: 0;
    }
    [class*="st-key-chatrow_"] .stButton > button p {
        overflow: hidden;
        white-space: nowrap;
        text-overflow: ellipsis;
        text-align: left;
        margin: 0;
    }
    [class*="st-key-chatrow_active_"] .stButton > button p {
        font-weight: 500;
    }
    /* Row menu stays out of the way until the row is hovered or active. */
    [class*="st-key-chatrow_"] .stPopover button {
        background: transparent;
        border: none;
        box-shadow: none;
        min-height: 28px;
        padding: 2px 6px;
    }
    [class*="st-key-chatrow_"] .stPopover {
        opacity: 0;
        transition: opacity 0.15s ease;
    }
    [class*="st-key-chatrow_"]:hover .stPopover,
    [class*="st-key-chatrow_active_"] .stPopover {
        opacity: 1;
    }
    /* New chat reads as a list row, not a loud CTA. */
    [class*="st-key-newchat"] button {
        justify-content: flex-start;
        border-radius: 10px;
        min-height: 40px;
    }
    [class*="st-key-newchat"] button > div {
        justify-content: flex-start;
        width: 100%;
    }
    /* Drop the popover's chevron so icon-only popovers stay a single glyph. */
    [class*="st-key-chatrow_"] [data-testid="stPopoverButton"] div[aria-hidden="true"],
    [class*="st-key-copy_"] [data-testid="stPopoverButton"] div[aria-hidden="true"] {
        display: none;
    }
    [class*="st-key-copy_"] [data-testid="stPopoverButton"] {
        background: transparent;
        border: none;
        box-shadow: none;
    }
</style>
""")

CHATS_BEFORE_FOLD = 10

TOOL_ICONS = {
    "tavily": ":material/travel_explore:",
    "search": ":material/travel_explore:",
    "calculator": ":material/calculate:",
    "weather": ":material/partly_cloudy_day:",
    "stock_price": ":material/trending_up:",
    "purchase": ":material/shopping_cart:",
    "retriever": ":material/description:",
}

TOOL_LABELS = {
    "tavily": ("Searching the web", "Searched the web"),
    "search": ("Searching the web", "Searched the web"),
    "calculator": ("Calculating", "Calculated"),
    "weather": ("Checking the weather", "Checked the weather"),
    "stock_price": ("Looking up the stock price", "Looked up the stock price"),
    "purchase": ("Placing the order", "Placed the order"),
    "retriever": ("Searching your documents", "Searched your documents"),
}

SUGGESTIONS = {
    ":blue[:material/travel_explore:] AI news this week":
        "What are the most important AI news stories from this week?",
    ":green[:material/calculate:] Crunch a number":
        "What is the compound interest on 25000 at 7.5% for 12 years?",
    ":orange[:material/partly_cloudy_day:] Weather in Lahore":
        "What is the current weather in Lahore?",
    ":violet[:material/trending_up:] Price of NVDA":
        "What is the current stock price of NVDA?",
}


def tool_meta(tool_name):
    """Map a raw tool name to (running label, done label, Material icon)."""
    name = (tool_name or "tool").lower()
    for key, icon in TOOL_ICONS.items():
        if key in name:
            running, done = TOOL_LABELS[key]
            return running, done, icon
    return f"Running {tool_name}", f"Used {tool_name}", ":material/build:"


def render_tool_result(container, content):
    try:
        container.json(json.loads(content))
    except Exception:
        container.markdown("```text\n" + str(content) + "\n```")


@st.cache_data(ttl=3600, show_spinner=False)
def load_models():
    return chat_store.available_models()


MODELS = load_models()


def apply_model():
    """Point the graph at the selected model. Cheap no-op when unchanged."""
    selected = next(
        (m for m in MODELS if m["id"] == st.session_state.model_id),
        MODELS[0],
    )
    chat_store.use_model(selected["id"], selected["provider"])


def new_thread_id():
    return str(uuid.uuid4())


def start_new_chat(rerun=True):
    thread_id = new_thread_id()
    st.session_state.thread_id = thread_id
    st.session_state.setdefault("histories", {})[thread_id] = []
    threads = st.session_state.setdefault("threads", [])
    if thread_id not in threads:
        threads.insert(0, thread_id)
    if rerun:
        st.rerun()


def init_state():
    if "model_id" not in st.session_state:
        known = [m["id"] for m in MODELS]
        st.session_state.model_id = (
            chat_store.active_model() if chat_store.active_model() in known else known[0]
        )
    # The backend process is shared across sessions, so re-assert the choice.
    apply_model()

    if "titles" not in st.session_state:
        st.session_state.titles = chat_store.load_titles()

    if "threads" not in st.session_state:
        st.session_state.threads = chat_store.list_threads_by_recency()

    if "histories" not in st.session_state:
        st.session_state.histories = {}

    if "thread_id" not in st.session_state:
        start_new_chat(rerun=False)

    thread_id = st.session_state.thread_id
    if thread_id not in st.session_state.histories:
        st.session_state.histories[thread_id] = chat_store.load_history(thread_id)

    backfill_titles()


def backfill_titles():
    """Give pre-existing threads a readable name.

    Titles are only generated when a chat starts, so threads created before the
    sidecar existed have none. Derive one from the first user message and
    persist it, so this walk happens at most once per thread ever.
    """
    if st.session_state.get("titles_backfilled"):
        return
    st.session_state.titles_backfilled = True

    missing = [t for t in st.session_state.threads if t not in st.session_state.titles]
    if not missing:
        return

    with st.spinner("Loading your chats"):
        for thread_id in missing:
            first = chat_store.first_user_message(thread_id)
            if not first:
                # Empty thread (e.g. this session's new chat); nothing to name yet.
                continue
            title = chat_store.fallback_title(first)
            st.session_state.titles[thread_id] = title
            chat_store.save_title(thread_id, title)


def history():
    return st.session_state.histories[st.session_state.thread_id]


def chat_title(thread_id):
    saved = st.session_state.titles.get(thread_id)
    if saved:
        return saved
    for msg in st.session_state.histories.get(thread_id, []):
        if msg["role"] == "user":
            return chat_store.fallback_title(msg["content"])
    return "New chat"


def matches_search(thread_id, query):
    if not query:
        return True
    query = query.lower()
    if query in chat_title(thread_id).lower():
        return True
    # Only search threads already pulled into memory; loading every thread from
    # SQLite on each keystroke would make the sidebar crawl.
    for msg in st.session_state.histories.get(thread_id, []):
        if query in str(msg.get("content", "")).lower():
            return True
    return False


def queue_prompt(text):
    st.session_state.pending_prompt = text
    st.rerun()


init_state()

# ============================== Sidebar ==============================

with st.sidebar:
    if st.button("New chat", key="newchat", type="secondary",
                 icon=":material/edit_square:", width="stretch"):
        start_new_chat()

    search_query = st.text_input(
        "Search chats",
        placeholder="Search chats",
        icon=":material/search:",
        label_visibility="collapsed",
    )

    st.caption("Recent")

    matching = [t for t in st.session_state.threads if matches_search(t, search_query)]

    if not matching:
        st.caption("No chats match that search." if search_query else "No chats yet.")

    # A search is already a filter, so show every hit; otherwise keep the list
    # short until the user asks for more.
    show_all = bool(search_query) or st.session_state.get("show_all_chats", False)
    visible_threads = matching if show_all else matching[:CHATS_BEFORE_FOLD]

    chat_list = st.container(key="chatlist", gap=None)

    for thread_id in visible_threads:
        is_active = thread_id == st.session_state.thread_id
        row_key = ("chatrow_active_" if is_active else "chatrow_") + thread_id

        with chat_list.container(key=row_key, horizontal=True, gap=None,
                                 vertical_alignment="center"):
            if st.button(chat_title(thread_id), key="open_" + thread_id, width="stretch"):
                st.session_state.thread_id = thread_id
                st.rerun()

            with st.popover("", icon=":material/more_horiz:", key="menu_" + thread_id):
                new_name = st.text_input(
                    "Rename chat",
                    value=chat_title(thread_id),
                    key="rename_" + thread_id,
                )
                if st.button("Save", key="save_" + thread_id,
                             icon=":material/check:", width="stretch"):
                    st.session_state.titles[thread_id] = new_name.strip() or "New chat"
                    chat_store.save_title(thread_id, st.session_state.titles[thread_id])
                    st.rerun()

                if st.button("Delete chat", key="del_" + thread_id,
                             icon=":material/delete:", width="stretch"):
                    chat_store.delete_thread(thread_id)
                    st.session_state.threads = [
                        t for t in st.session_state.threads if t != thread_id
                    ]
                    st.session_state.histories.pop(thread_id, None)
                    st.session_state.titles.pop(thread_id, None)
                    if is_active:
                        start_new_chat(rerun=False)
                    st.rerun()

    hidden_count = len(matching) - len(visible_threads)
    if not search_query and (hidden_count > 0 or st.session_state.get("show_all_chats")):
        if hidden_count > 0:
            if st.button("See more (%d)" % hidden_count, key="seemore",
                         icon=":material/expand_more:", type="tertiary", width="stretch"):
                st.session_state.show_all_chats = True
                st.rerun()
        elif st.button("Show less", key="seeless", icon=":material/expand_less:",
                       type="tertiary", width="stretch"):
            st.session_state.show_all_chats = False
            st.rerun()

    st.space("small")

    model_labels = {m["id"]: "%s · %s" % (m["label"], m["note"]) for m in MODELS}
    model_ids = list(model_labels.keys())
    if st.session_state.model_id not in model_ids:
        # Catalog changed under us (cache expiry, key removed); fall back.
        st.session_state.model_id = model_ids[0]
    selected_model = st.selectbox(
        "Model",
        model_ids,
        index=model_ids.index(st.session_state.model_id),
        format_func=lambda mid: model_labels[mid],
        key="model_select",
    )
    if selected_model != st.session_state.model_id:
        st.session_state.model_id = selected_model
        apply_model()
        st.rerun()

    with st.expander("Agent tools", icon=":material/build:"):
        for tool in tools:
            tool_name = getattr(tool, "name", str(tool))
            _, _, icon = tool_meta(tool_name)
            st.badge(tool_name, icon=icon, color="gray")

# ============================== Main area ==============================

messages = history()
suggested_prompt = None

with st.container(horizontal=True, vertical_alignment="center"):
    st.title("Agentic chatbot", width="stretch")
    st.badge(st.session_state.model_id, icon=":material/bolt:", color="primary")

if not messages:
    with st.container(horizontal_alignment="center"):
        st.markdown("# :material/smart_toy:")
        st.title("How can I help?", text_alignment="center")
        st.caption(
            "Search the web, crunch numbers, read your PDFs, and check markets and weather.",
            text_alignment="center",
        )
    st.space("small")
    picked = st.pills(
        "Try asking",
        list(SUGGESTIONS.keys()),
        label_visibility="collapsed",
        key="suggestions_" + st.session_state.thread_id,
    )
    if picked:
        # Handled in this same run rather than via a rerun: the pills keep their
        # selection across reruns, so re-queueing would loop forever.
        suggested_prompt = SUGGESTIONS[picked]

# Group consecutive messages from the same role into a single bubble.
groups = []
for message in messages:
    if not groups or groups[-1]["role"] != message["role"]:
        groups.append({"role": message["role"], "items": [message]})
    else:
        groups[-1]["items"].append(message)

for group_index, group in enumerate(groups):
    avatar = ":material/smart_toy:" if group["role"] == "assistant" else None
    with st.chat_message(group["role"], avatar=avatar):
        last_text = None
        for item in group["items"]:
            if item.get("type") == "tool":
                _, done_label, icon = tool_meta(item.get("tool_name"))
                with st.status(icon + " " + done_label, state="complete", type="compact") as box:
                    render_tool_result(box, item["content"])
            else:
                st.markdown(item["content"])
                last_text = item["content"]

        if group["role"] == "assistant" and last_text:
            with st.container(horizontal=True, gap="small", vertical_alignment="center"):
                with st.popover("", icon=":material/content_copy:",
                                key="copy_%d" % group_index, help="Copy"):
                    st.code(last_text, language=None, wrap_lines=True)

                if st.button("", key="regen_%d" % group_index, icon=":material/refresh:",
                             type="tertiary", help="Ask again"):
                    previous_user = next(
                        (g["items"][-1]["content"]
                         for g in reversed(groups[:group_index]) if g["role"] == "user"),
                        None,
                    )
                    if previous_user:
                        queue_prompt(previous_user)

                st.feedback(
                    "thumbs",
                    key="feedback_%s_%d" % (st.session_state.thread_id, group_index),
                )

# ============================== Input and approvals ==============================

CONFIG = {
    "configurable": {"thread_id": st.session_state.thread_id},
    "metadata": {"thread_id": st.session_state.thread_id},
    "run_name": "chat_trace",
}

state_snapshot = chatbot.get_state(CONFIG)
interrupt_text = chat_store.pending_interrupt(state_snapshot)
is_interrupted = interrupt_text is not None

user_input = st.session_state.pop("pending_prompt", None) or suggested_prompt
uploaded_files = []

if is_interrupted:
    with st.chat_message("assistant", avatar=":material/smart_toy:"):
        with st.container(border=True):
            st.warning(
                interrupt_text or "The agent needs your approval to continue.",
                icon=":material/pending_actions:",
            )
            with st.container(horizontal=True, horizontal_alignment="distribute"):
                if st.button("Approve", type="primary",
                             icon=":material/check:", width="stretch"):
                    user_input = "yes"
                if st.button("Decline", icon=":material/close:", width="stretch"):
                    user_input = "no"

    st.chat_input("Waiting for your decision", disabled=True)
else:
    submitted = st.chat_input(
        "Message the agent",
        accept_file="multiple",
        file_type=["pdf"],
        submit_mode="stop",
    )

    if submitted:
        if isinstance(submitted, str):
            user_input = submitted
        else:
            user_input = getattr(submitted, "text", "") or user_input
            uploaded_files = getattr(submitted, "files", []) or []

for uploaded_file in uploaded_files:
    with st.chat_message("assistant", avatar=":material/smart_toy:"):
        with st.status("Reading " + uploaded_file.name, expanded=True) as status:
            tmp_path = None
            try:
                with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp_file:
                    tmp_file.write(uploaded_file.getvalue())
                    tmp_path = tmp_file.name

                st.write("Extracting and embedding text")
                ingest_rag_document(tmp_path)
                result = "Added **" + uploaded_file.name + "** to the knowledge base."
                status.update(label=result, state="complete", expanded=False)
            except Exception as e:
                result = "Could not process **" + uploaded_file.name + "**: " + str(e)
                status.update(label=result, state="error", expanded=False)
            finally:
                if tmp_path and os.path.exists(tmp_path):
                    os.remove(tmp_path)

    messages.append({"role": "assistant", "type": "text", "content": result})

# ============================== Turn execution ==============================


def filter_think(stream, tool_area):
    """Yield visible assistant text, hiding <think> blocks and surfacing tools.

    Tool UI goes into tool_area, a container made before st.write_stream starts.
    Creating elements inline here instead would land them in the middle of the
    container write_stream is streaming into, and the text stops updating live.
    """
    in_think = False
    buffer = ""
    status_container = None

    for message_chunk, metadata in stream:
        if (isinstance(message_chunk, AIMessage)
                and getattr(message_chunk, "tool_call_chunks", None)):
            for tc in message_chunk.tool_call_chunks:
                if tc.get("name"):
                    running, _, icon = tool_meta(tc["name"])
                    running_label = icon + " " + running
                    if status_container is None:
                        status_container = tool_area.status(running_label, expanded=True,
                                                            type="compact")
                    else:
                        status_container.update(label=running_label, expanded=True)

        elif message_chunk.__class__.__name__ == "ToolMessage":
            tool_name = getattr(message_chunk, "name", "tool")
            _, done, icon = tool_meta(tool_name)
            done_label = icon + " " + done

            if status_container is not None:
                status_container.update(label=done_label, state="complete", expanded=False)
                render_tool_result(status_container, message_chunk.content)
                status_container = None
            else:
                with tool_area.status(done_label, state="complete", type="compact") as box:
                    render_tool_result(box, message_chunk.content)

            messages.append({
                "role": "assistant",
                "type": "tool",
                "tool_name": tool_name,
                "content": message_chunk.content,
            })

        if isinstance(message_chunk, AIMessage) and message_chunk.content:
            buffer += message_chunk.content
            while True:
                if not in_think:
                    start_idx = buffer.find("<think>")
                    if start_idx != -1:
                        if start_idx > 0:
                            yield buffer[:start_idx]
                        buffer = buffer[start_idx + len("<think>"):]
                        in_think = True
                    else:
                        if len(buffer) > 6:
                            yield buffer[:-6]
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

    if not in_think and buffer:
        yield buffer


if user_input:
    thread_id = st.session_state.thread_id
    is_first_message = len(messages) == 0

    if not is_interrupted:
        messages.append({"role": "user", "type": "text", "content": user_input})
        with st.chat_message("user"):
            st.markdown(user_input)

    if is_first_message:
        title = chat_store.generate_title(user_input)
        st.session_state.titles[thread_id] = title
        chat_store.save_title(thread_id, title)

    with st.chat_message("assistant", avatar=":material/smart_toy:"):
        stream_input = (
            Command(resume=user_input)
            if is_interrupted
            else {"messages": [HumanMessage(content=user_input)]}
        )
        tool_area = st.container()
        ai_message = st.write_stream(
            filter_think(
                chatbot.stream(stream_input, config=CONFIG, stream_mode="messages"),
                tool_area,
            )
        )

    if ai_message:
        messages.append({"role": "assistant", "type": "text", "content": ai_message})

    if thread_id not in st.session_state.threads:
        st.session_state.threads.insert(0, thread_id)

    st.rerun()
