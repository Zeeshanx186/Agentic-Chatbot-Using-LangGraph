"""Frontend-side persistence and model-catalog helpers for the chatbot UI.

The Chainlit app calls bind() with the backend module before use.
"""

from langchain_core.messages import AIMessage, HumanMessage

import datetime
import json
import os
import re
import requests
from dexter import cloud_store

# Runtime files stay at the project root (parent of this package), which is
# /app inside the image and the repo root when you launch from there.
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TITLES_PATH = os.path.join(_PROJECT_ROOT, "chat_titles.json")
FEEDBACK_PATH = os.path.join(_PROJECT_ROOT, "chat_feedback.json")

THINK_RE = re.compile(r"<think>.*?</think>", flags=re.DOTALL)

backend = None
chatbot = None
checkpoint = None


def bind(backend_module):
    """Point these helpers at one backend. Call once, before anything else."""
    global backend, chatbot, checkpoint
    backend = backend_module
    chatbot = backend_module.chatbot
    checkpoint = backend_module.checkpoint


def _require_backend():
    if backend is None:
        raise RuntimeError(
            "chat_store is unbound - call chat_store.bind(<your backend module>) "
            "at the top of the app before using it."
        )


def strip_think(text):
    """Remove <think>...</think> blocks that reasoning models emit."""
    return THINK_RE.sub("", text or "").strip()


# ---------------------------------------------------------------- threads

def list_threads_by_recency():
    """Thread ids, most recently touched first.

    SqliteSaver.list() yields newest checkpoints first, so de-duplicating while
    preserving first-seen order gives a stable, recency-sorted list. The
    backend's get_all_threads() builds a set, which reshuffles the sidebar on
    every restart.
    """
    return [summary["id"] for summary in thread_summaries()]


def thread_summaries():
    """Thread id, last-activity time and first-message preview, newest first.

    All three come from one pass over the checkpoint log. A thread's newest
    checkpoint already carries its whole message list, so the first human turn
    is right there - no need for a get_state per thread, which would put ~90
    SQLite round-trips in front of every sidebar rerun. Measured at 0.19s for 87
    threads against ~6MB of checkpoints.

    `updated` is a naive UTC datetime, or None when a checkpoint predates the ts
    field; callers group on it and must tolerate the gap.
    """
    _require_backend()
    summaries = []
    seen = set()

    for ckpt in checkpoint.list(None):
        thread_id = ckpt.config.get("configurable", {}).get("thread_id")
        if not thread_id or thread_id in seen:
            continue
        seen.add(thread_id)

        stamp = ckpt.checkpoint.get("ts")
        try:
            # Stored UTC, grouped into Today/Yesterday buckets against the
            # user's own clock - so convert before dropping the offset, or a
            # late-evening chat lands in tomorrow.
            updated = datetime.datetime.fromisoformat(stamp).astimezone().replace(tzinfo=None)
        except (TypeError, ValueError):
            updated = None

        messages = (ckpt.checkpoint.get("channel_values") or {}).get("messages") or []
        first = next((m for m in messages if isinstance(m, HumanMessage)), None)
        preview = strip_file_note(first.content) if first is not None else ""

        summaries.append({
            "id": thread_id,
            "updated": updated,
            "preview": " ".join(preview.split()),
        })

    return summaries


def delete_thread(thread_id):
    """Drop every checkpoint for a thread plus its saved title."""
    _require_backend()
    try:
        checkpoint.delete_thread(thread_id)
    except Exception:
        pass
    delete_title(thread_id)


# ----------------------------------------------------------------- titles

def _read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_json(path, data):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception:
        pass


def load_titles():
    if cloud_store.ENABLED:
        return cloud_store.metadata_all("titles")
    return _read_json(TITLES_PATH)


def _write_titles(titles):
    _write_json(TITLES_PATH, titles)


def save_title(thread_id, title):
    if cloud_store.ENABLED:
        return cloud_store.metadata_set("titles", thread_id, title)
    titles = load_titles()
    titles[thread_id] = title
    _write_titles(titles)


def delete_title(thread_id):
    if cloud_store.ENABLED:
        return cloud_store.metadata_set("titles", thread_id, None)
    titles = load_titles()
    if titles.pop(thread_id, None) is not None:
        _write_titles(titles)


def generate_title(first_message):
    """Ask the model for a short title for a brand new conversation."""
    try:
        # Read through the module so a model swap via set_model() is picked up.
        res = backend.llm.invoke(
            "Provide a short title (max 4 words) for the conversation that starts "
            f"with: '{first_message}'. Only reply with the title itself."
        )
        title = strip_think(res.content).strip("\"' \n")
        return title or fallback_title(first_message)
    except Exception:
        return fallback_title(first_message)


def fallback_title(first_message):
    text = (first_message or "New chat").strip()
    return text[:28] + "..." if len(text) > 28 else text


# --------------------------------------------------------------- feedback

def load_feedback():
    """Thumbs ratings, as {thread_id: {message_index: 0|1}}."""
    if cloud_store.ENABLED:
        feedback = {}
        for key, value in cloud_store.metadata_all("feedback").items():
            thread, index = key.rsplit(":", 1)
            feedback.setdefault(thread, {})[index] = value
        return feedback
    return _read_json(FEEDBACK_PATH)


def save_feedback(thread_id, index, value):
    """Record one thumbs rating. A cleared rating drops the entry."""
    if cloud_store.ENABLED:
        return cloud_store.metadata_set("feedback", f"{thread_id}:{index}", value)
    feedback = load_feedback()
    thread = feedback.setdefault(thread_id, {})
    if value is None:
        thread.pop(str(index), None)
    else:
        thread[str(index)] = value
    if not thread:
        feedback.pop(thread_id, None)
    _write_json(FEEDBACK_PATH, feedback)


# ---------------------------------------------------------------- history

# Both shapes the UI has prepended to a user message for the agent's benefit -
# the current listing form and the older comma-joined one still sitting in old
# threads.
_FILE_NOTE = re.compile(
    r"\A(?:\d+\s+file\(s\)\s+are\s+ALREADY\s+SAVED"
    r"|\d+\s+document\(s\)\s+are\s+ALREADY\s+INDEXED"
    r"|Attached\s+file\(s\):).*?(?:\n\n|\Z)",
    re.S | re.I,
)


# The listing form puts one path per "- " line; the legacy form comma-joins them
# after the colon.
_NOTE_PATH_LINE = re.compile(r"^[ \t]*-[ \t]+(\S.*?)[ \t]*$", re.M)

# The indexed-document note names its documents inline, after "available now:".
_INDEXED_NAMES = re.compile(r"available now:\s*(.+?)\.\s", re.S)

_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg"}


TITLE_WIDTH = 28


def _shorten(text, width=TITLE_WIDTH):
    """Trim a filename to title width from the middle, not the end.

    Chopping the tail is wrong for the names that need this most: five uploads
    here begin "DSP-ENG-GCS-23E006-IN-IOL-0001" and differ only after it, so a
    prefix-truncated title recreates the collision it was meant to fix. Keeping
    both ends preserves whichever half carries the difference.
    """
    text = text.strip()
    if len(text) <= width:
        return text
    head = (width - 3) // 2
    tail = width - 3 - head
    return text[:head] + "..." + text[-tail:]


def title_from_attachments(paths):
    """Name a thread after the file it opened with, or "" if it opened with none.

    Every "what is in this image" reads identically to a titling model, which is
    how fourteen threads ended up sharing four interchangeable names. The
    filename is the part that actually differs between them, so use it directly
    and skip the LLM round-trip entirely.
    """
    names = []
    for path in paths:
        stem = os.path.splitext(os.path.basename(path))[0]
        # The _1/_2 counter save_upload appends is deliberately kept. It looks
        # like noise until you notice the same file gets uploaded repeatedly -
        # five threads here opened with RFQ_Review_Checklist - and then it is
        # the only thing telling those threads apart. Stripping it turned 31
        # duplicate-titled threads into 34.
        stem = re.sub(r"_+", " ", stem).strip()
        if stem:
            names.append(_shorten(stem))

    if not names:
        return ""
    if len(names) == 1:
        return names[0]
    return _shorten("%s +%d more" % (names[0], len(names) - 1))


def attachment_paths(text):
    """Paths of the files attached to a user message, read back out of the note.

    The note the UI prepends for the agent already lists every path, and unlike
    the UI's own message list it survives in the checkpoint. So a thread reopened
    after a restart can rebuild its attachments from what is already stored,
    rather than from a second sidecar file that would have to be kept in sync
    with uploads/ and pruned by hand.
    """
    match = _FILE_NOTE.match(str(text))
    if not match:
        return []

    note = match.group(0)
    paths = _NOTE_PATH_LINE.findall(note)
    if not paths and note.lower().startswith("attached file(s):"):
        # Legacy "Attached file(s): a, b" form. Windows paths carry no commas,
        # so splitting on one is safe here. Anchored on that exact opening
        # because the indexed-document note also has a colon, and splitting on
        # it handed back half a sentence as though it were a file on disk.
        paths = note.split(":", 1)[1].split(",")

    return [path for path in (p.strip() for p in paths) if path]


def attached_images(text):
    """Image attachments of a user message that are still on disk.

    Existence is checked because uploads/ is gitignored scratch space the user
    may clear at any time - a path that no longer resolves would make st.image
    raise while redrawing an old thread.
    """
    return [
        path for path in attachment_paths(text)
        if os.path.splitext(path)[1].lower() in _IMAGE_EXTENSIONS
        and os.path.exists(cloud_store.restore_path(path))
    ]


def indexed_note(names):
    """The note that tells the agent an uploaded PDF is already in the index.

    A PDF does not stay on disk: it is chunked into FAISS and the temp file is
    deleted, so it never reaches the ALREADY SAVED note, which is built from
    paths. That left an indexed document completely invisible to the model - on
    2026-08-24 the supervisor routed "what is this audit instruction file" to
    Research_Agent, retriever_tool was bound to the call, and the agent still
    answered "I don't see any file attached to your message" because nothing in
    the turn said a document existed.

    Names the documents and the job, never the tool: naming a tool in this note
    is what once made the supervisor pick an agent by which tools the sentence
    mentioned rather than by what the files were.
    """
    names = [name for name in names if name]
    if not names:
        return ""
    return (
        "%d document(s) are ALREADY INDEXED in the searchable knowledge base "
        "and available now: %s. Never ask the user to upload them again. The "
        "job is to answer from the text inside those documents."
    ) % (len(names), ", ".join(names))


def strip_file_note(text):
    """Drop the attachment note the UI prepends to a message for the agent.

    That note is written for the model: absolute paths plus an instruction not
    to ask for a re-upload. Live, the UI never shows it - it renders the user's
    own sentence instead. A thread rebuilt from the checkpoint has only the raw
    HumanMessage, so without this the bubble reverts to a wall of Windows path
    the moment you switch threads and come back.
    """
    text = str(text)
    stripped = _FILE_NOTE.sub("", text, count=1).strip()
    if stripped:
        return stripped

    # The note was the whole message - an attachment sent with no question.
    # Name the files rather than showing nothing.
    names = [os.path.basename(line[2:].strip())
             for line in text.splitlines() if line.startswith("- ")]
    if not names:
        # The indexed-document note carries its names inline; it has no paths to
        # list because the PDF lives in FAISS rather than on disk.
        inline = _INDEXED_NAMES.search(text)
        if inline:
            names = [n.strip() for n in inline.group(1).split(",") if n.strip()]
    return "Attached " + ", ".join(names) if names else "Attached files"


def load_history(thread_id):
    """Rebuild the UI message list for a thread from the LangGraph checkpoint."""
    _require_backend()
    state = chatbot.get_state({"configurable": {"thread_id": thread_id}})
    history = []

    for m in state.values.get("messages", []):
        if isinstance(m, HumanMessage):
            # Images first, matching the order the live turn appends them, so a
            # reopened thread looks the same as the one you just typed into.
            for path in attached_images(m.content):
                history.append({"role": "user", "type": "image", "content": path})
            history.append({"role": "user", "type": "text",
                            "content": strip_file_note(m.content)})
        elif isinstance(m, AIMessage):
            content = strip_think(m.content)
            if content:
                history.append({"role": "assistant", "type": "text", "content": content})
        elif m.__class__.__name__ == "ToolMessage":
            history.append({
                "role": "assistant",
                "type": "tool",
                "tool_name": getattr(m, "name", "tool"),
                "content": m.content,
            })

    return history


def pending_interrupt(state):
    """Return the interrupt prompt if the graph is paused, else None."""
    if state.next and state.tasks and state.tasks[0].interrupts:
        return str(state.tasks[0].interrupts[0].value)
    return None



def first_user_message(thread_id):
    """Cheap-ish title source: the first human message of a thread, if any."""
    for msg in load_history(thread_id):
        if msg["role"] == "user":
            return msg["content"]
    return None


# ----------------------------------------------------------------- models

# Families the chat graph cannot drive: speech, safety classifiers, and Groq's
# compound systems (they carry their own built-in tools instead of ours).
_EXCLUDED_MODEL_MARKERS = (
    "whisper", "orpheus", "prompt-guard", "safeguard", "guard", "compound", "tts",
)

# Shown when the Groq catalog cannot be reached.
_FALLBACK_GROQ_MODELS = ["qwen/qwen3.6-27b", "openai/gpt-oss-120b", "openai/gpt-oss-20b"]

# Free tier via Google AI Studio; only offered when a key is configured.
_GOOGLE_MODELS = ["gemini-2.5-flash", "gemini-2.5-flash-lite", "gemini-2.0-flash"]


def _pretty(model_id):
    return model_id.split("/")[-1].replace("-", " ")


def _groq_model_ids():
    key = os.environ.get("GROQ_API_KEY")
    if not key:
        return []
    try:
        response = requests.get(
            "https://api.groq.com/openai/v1/models",
            headers={"Authorization": "Bearer " + key},
            timeout=10,
        )
        response.raise_for_status()
        ids = []
        for entry in response.json().get("data", []):
            model_id = entry.get("id", "")
            if not entry.get("active", True):
                continue
            if (entry.get("context_window") or 0) < 8192:
                continue
            if any(marker in model_id.lower() for marker in _EXCLUDED_MODEL_MARKERS):
                continue
            ids.append(model_id)
        return sorted(ids)
    except Exception:
        return []


def _openrouter_free_models():
    """OpenRouter models that cost nothing and can actually run this app.

    Three filters, all load-bearing:

    * price zero on both prompt and completion - OpenRouter lists 420 models and
      only ~20 are free, so anything else would quietly start billing.
    * tool calling - every node here binds tools, so a model without it fails on
      the first turn rather than degrading.
    * text output - the free list includes audio and video models that would
      never return an answer this UI can render.

    Queried live for the same reason the Groq list is: the free roster changes
    week to week, and a hardcoded list would offer models that have started
    charging or vanished.
    """
    if not os.environ.get("OPENROUTER_API_KEY"):
        return []
    try:
        response = requests.get("https://openrouter.ai/api/v1/models", timeout=10)
        response.raise_for_status()

        free = []
        for entry in response.json().get("data", []):
            pricing = entry.get("pricing") or {}
            try:
                if float(pricing.get("prompt") or 1) or float(pricing.get("completion") or 1):
                    continue
            except (TypeError, ValueError):
                continue

            architecture = entry.get("architecture") or {}
            if "text" not in (architecture.get("output_modalities") or []):
                continue
            if "tools" not in (entry.get("supported_parameters") or []):
                continue

            free.append((entry.get("context_length") or 0, entry.get("id", "")))

        # Widest context first: escaping Groq's 8000-tokens-per-minute ceiling is
        # the reason to reach for these at all.
        free.sort(reverse=True)
        return [model_id for _context, model_id in free if model_id]
    except Exception:
        return []


def _ollama_models():
    """Models pulled locally in Ollama."""
    host = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")
    try:
        response = requests.get(f"{host}/api/tags", timeout=1.0)
        response.raise_for_status()
        models = []
        for entry in response.json().get("models", []):
            name = entry.get("name") or entry.get("model")
            if name:
                models.append(name)
        return sorted(models)
    except Exception:
        return []


def available_models():
    """Chat models this install can actually call, newest catalog first.

    Groq is queried live so the list never goes stale. Google entries appear
    only when GOOGLE_API_KEY is set, since they need a separate free key from
    AI Studio. Ollama is queried locally if running.
    """
    models = []

    for model_id in _ollama_models():
        models.append({
            "id": model_id,
            "provider": "ollama",
            "label": _pretty(model_id),
            "note": "Ollama · local",
        })

    for model_id in (_groq_model_ids() or _FALLBACK_GROQ_MODELS):
        models.append({
            "id": model_id,
            "provider": "groq",
            "label": _pretty(model_id),
            "note": "Groq · free tier",
        })

    if os.environ.get("GOOGLE_API_KEY"):
        for model_id in _GOOGLE_MODELS:
            models.append({
                "id": model_id,
                "provider": "google",
                "label": _pretty(model_id),
                "note": "Google AI Studio · free tier",
            })

    for model_id in _openrouter_free_models():
        models.append({
            "id": model_id,
            "provider": "openrouter",
            # The ":free" suffix is part of the id and must stay in it, but in a
            # picker where every entry is free it is noise on the label.
            "label": _pretty(model_id.removesuffix(":free")),
            "note": "OpenRouter · free",
        })

    if not models:
        models.append({
            "id": backend.DEFAULT_MODEL,
            "provider": "groq",
            "label": _pretty(backend.DEFAULT_MODEL),
            "note": "Groq · free tier",
        })

    return models


def use_model(model_id, provider="groq"):
    backend.set_model(model_id, provider)


def active_model():
    return backend.current_model
