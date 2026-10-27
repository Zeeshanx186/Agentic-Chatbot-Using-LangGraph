# Standard Libra
import datetime
import ipaddress
import os
import sqlite3
import json
import re
import uuid
import requests
from dexter import cloud_store
import socket
import urllib.parse
from zoneinfo import ZoneInfo
from typing import Annotated, TypedDict, Literal
import sys
from pathlib import Path

# Keep `import dexter` working when this file is loaded outside the repo root.
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# Third-party utilities
from pydantic import BaseModel, Field
from dotenv import load_dotenv
import pandas as pd
import openpyxl


# LangChain Core & Community
from langchain_core.messages import (
    BaseMessage, SystemMessage, HumanMessage, ToolMessage, AIMessage, trim_messages,
)
from langchain_core.messages.utils import count_tokens_approximately
from langchain_core.tools import tool
# PyPDFLoader, FAISS, the text splitter and HuggingFaceEmbeddings are imported
# lazily inside the RAG functions. Each one drags in sentence_transformers and
# torch (~14s) at import time, which every non-RAG chat would otherwise wait
# for on startup.
from groq import GroqError
from langchain_groq import ChatGroq
from langchain_tavily import TavilySearch
from langchain_community.utilities import WikipediaAPIWrapper
from langchain_community.tools import DuckDuckGoSearchRun

# LangGraph
from langgraph.graph import START, END, StateGraph
from langgraph.graph.message import add_messages
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.prebuilt import ToolNode
from langgraph.types import interrupt, Command
from dexter.google_tools import GOOGLE_TOOLS


load_dotenv()

GROQ_GENERATION_OPTIONS = (
    {"max_tokens": int(os.environ["GROQ_MAX_TOKENS"])}
    if os.environ.get("GROQ_MAX_TOKENS") else {}
)
llm = ChatGroq(model="qwen/qwen3.6-27b", reasoning_effort="none", temperature=0, **GROQ_GENERATION_OPTIONS)
DB_PATH = "faiss_db"

# Building HuggingFaceEmbeddings costs ~27s: 15s to import torch and load the
# model, plus ~12s of Hub freshness checking. Doing it at import time made the
# whole app - including chats that never touch RAG - wait for it, and made a
# missing sentence-transformers install a hard startup failure. Build it on
# first use instead.
_embeddings = None
_retriever = None


def get_embeddings():
    global _embeddings
    if _embeddings is None:
        from langchain_huggingface import HuggingFaceEmbeddings

        try:
            # The model never changes, so skip the network round-trip.
            _embeddings = HuggingFaceEmbeddings(
                model_name="sentence-transformers/all-MiniLM-L6-v2",
                model_kwargs={"local_files_only": True},
            )
        except Exception:
            # Not in the local cache yet - let it download this once.
            _embeddings = HuggingFaceEmbeddings(
                model_name="sentence-transformers/all-MiniLM-L6-v2",
            )
    return _embeddings

def has_documents():
    if cloud_store.ENABLED:
        return cloud_store.get_object("rag/index") is not None
    return os.path.exists(os.path.join(DB_PATH, "index.faiss"))

# tempfile.NamedTemporaryFile names, which is what PyPDFLoader records as the
# source when the UI streams an upload through /Temp. "tmpz6g_zkw1.pdf" is not a
# document name any user would recognise, so it is never shown.
_TEMP_NAME = re.compile(r"\Atmp[0-9a-z_]{6,}\.[a-z0-9]+\Z", re.I)


def _stamp_source(chunks, display_name):
    """Record the name the user uploaded, not the temp file it arrived in.

    Without this every chunk's source is a path in /Temp, so a retrieved passage
    cannot say which document it came from - and "what is this audit instruction
    file" has no answer even when the right chunks come back.
    """
    if not display_name:
        return chunks
    for chunk in chunks:
        chunk.metadata["source"] = display_name
    return chunks


def _doc_name(doc):
    """Displayable document name for a retrieved chunk, or "" if there is none."""
    source = str((doc.metadata or {}).get("source") or "")
    name = os.path.basename(source)
    return "" if not name or _TEMP_NAME.match(name) else name


def _format_docs(docs):
    """Retrieved chunks as text the model can quote and attribute."""
    results = []
    for i, doc in enumerate(docs):
        name = _doc_name(doc)
        page = (doc.metadata or {}).get("page")
        label = "Document %d" % (i + 1)
        if name:
            label += " - %s" % name
        if isinstance(page, int):
            # PyPDF pages are 0-based; the user counts from 1.
            label += ", page %d" % (page + 1)
        results.append("%s:\n%s\n" % (label, doc.page_content))
    return "\n\n".join(results)


def ingest_rag_document(file_path, display_name=None):
    from langchain_community.document_loaders import PyPDFLoader
    from langchain_community.vectorstores import FAISS
    from langchain_text_splitters import RecursiveCharacterTextSplitter

    global _retriever
    loader = PyPDFLoader(file_path)
    docs = loader.load()
    splitter = RecursiveCharacterTextSplitter(chunk_size = 1000, chunk_overlap = 200)
    chunks = _stamp_source(splitter.split_documents(docs),
                           display_name or os.path.basename(file_path))

    embeddings = get_embeddings()
    if cloud_store.ENABLED:
        with cloud_store.rag_transaction() as conn:
            stored = cloud_store.get_object("rag/index", conn)
            if stored:
                vectorstore = FAISS.deserialize_from_bytes(
                    bytes(stored["data"]), embeddings, allow_dangerous_deserialization=True)
                vectorstore.add_documents(chunks)
            else:
                vectorstore = FAISS.from_documents(chunks, embeddings)
            cloud_store.put_object("rag/index", vectorstore.serialize_to_bytes(), conn=conn)
        return
    if has_documents():
        # from_documents() + save_local() writes a brand new index, silently
        # dropping every PDF uploaded before this one. Merge into the existing
        # index instead.
        vectorstore = FAISS.load_local(
            folder_path=DB_PATH, embeddings=embeddings,
            allow_dangerous_deserialization=True,
        )
        vectorstore.add_documents(chunks)
    else:
        vectorstore = FAISS.from_documents(chunks, embeddings)

    vectorstore.save_local(DB_PATH)
    _retriever = None  # next query reloads the index it just grew

def get_retriever():
    """Cached retriever. Reloading FAISS from disk on every tool call is pure
    waste - ingest_rag_document() clears this when the index changes."""
    from langchain_community.vectorstores import FAISS

    global _retriever
    if cloud_store.ENABLED:
        stored = cloud_store.get_object("rag/index")
        vectorstore = FAISS.deserialize_from_bytes(
            bytes(stored["data"]), get_embeddings(), allow_dangerous_deserialization=True)
        return vectorstore.as_retriever(search_kwargs={"k": 5})
    if _retriever is None:
        vectorstore = FAISS.load_local(
            folder_path=DB_PATH, embeddings=get_embeddings(),
            allow_dangerous_deserialization=True,
        )
        _retriever = vectorstore.as_retriever(search_type="similarity", search_kwargs={"k": 5})
    return _retriever

# Read by the model, so it says what to do next. The old text - "No relevant
# information found related to the RAG Guide" - named a document the user had
# never mentioned, and the model read it as proof their own file was missing.
_RAG_MISS = (
    "No passage in the uploaded documents matched that query. Ask it again in "
    "different words, or with a term from the document. Never ask the user to "
    "upload the file again - it is already indexed."
)


@tool
def retriever_tool(query:str)->str:
    """Search every PDF the user has uploaded to this app's knowledge base and return the matching passages - use this for ANY question about an uploaded, attached or indexed document, whatever that document is named.

    Line 1 is the whole reason this tool gets called: it is all TOOL_GUIDE and
    the bound tool schema carry. It used to read "information from the RAG Guide
    PDF", naming one specific document, and on 2026-08-24 an indexed
    AUDIT_INSTRUCTIONS.pdf went unread - the tool was bound to the call, the
    model saw a tool for some other document, and it asked the user to upload
    the file they had just uploaded. Never name a single document here.
    """

    if not has_documents():
        return "No document has been uploaded yet. Ask the user to upload a PDF first."

    docs = get_retriever().invoke(query)

    if not docs:
        return _RAG_MISS

    return _format_docs(docs)

# Tools
search_tool = TavilySearch(max_results=5, topic="general", search_depth="advanced")
ddg = DuckDuckGoSearchRun()

# str(tavily_response) put the entire result dict - 5 advanced-depth pages of
# scraped text, plus scores and request ids - into the message history, where it
# was resent on every following LLM call. Two searches in one turn was enough to
# blow past Groq's 8000 tokens-per-minute limit with a 413. Digest instead.
SEARCH_RESULTS_KEPT = 3
SEARCH_SNIPPET_CHARS = 700

# Tavily ranks by relevance, not by whether the page is a source worth quoting,
# and only the first SEARCH_RESULTS_KEPT survive the digest. Asked for the
# temperature in Islamabad it returned two live forecast services and an undated
# Facebook post; the post sorted first, so the agent answered 21C from a winter
# post while the live services said 31C and 25C - a 48% error in the final
# number. Demote the domains that carry no date and no editorial process.
_LOW_TRUST_DOMAINS = (
    "facebook.com", "scribd.com", "pinterest.", "quora.com",
    "reddit.com", "x.com", "twitter.com", "tiktok.com",
)


def _is_low_trust(url) -> bool:
    return any(domain in (url or "") for domain in _LOW_TRUST_DOMAINS)


# Queries a real tool answers better than any search engine can. Deliberately
# narrow: these match a request for a LIVE reading, not background reading. "the
# climate of Islamabad" or "why NVIDIA stock rose" stay searchable.
_TOOL_OWNED_QUERIES = (
    (re.compile(r"\b(current|now|today|live|right now)\b.{0,30}\b(weather|temperature)\b"
                r"|\b(weather|temperature)\b.{0,30}\b(current|now|today|live|right now)\b"
                r"|\bweather in\b", re.IGNORECASE), "get_weather"),
    (re.compile(r"\b(stock|share)\s+price\b|\bstock\s+quote\b|\bprice\s+of\s+\w+\s+stock\b"
                r"|\b[A-Z]{1,5}\s+stock\s+price\b", re.IGNORECASE), "get_stock_price"),
)


def _tool_owned_query(query):
    for pattern, owner in _TOOL_OWNED_QUERIES:
        if pattern.search(query or ""):
            return owner
    return None


def _digest_search_results(results) -> str:
    # sorted() is stable, so this only pushes low-trust entries behind the
    # others and otherwise leaves Tavily's own ranking intact.
    ranked = sorted(results, key=lambda entry: _is_low_trust(entry.get("url")))
    chunks = []
    for entry in ranked[:SEARCH_RESULTS_KEPT]:
        content = " ".join((entry.get("content") or "").split())
        if len(content) > SEARCH_SNIPPET_CHARS:
            content = content[:SEARCH_SNIPPET_CHARS] + "..."
        chunks.append(
            "%s\n%s\n%s" % (entry.get("title", "Untitled"), entry.get("url", ""), content)
        )
    return "\n\n".join(chunks)


@tool
def web_search(query: str) -> str:
    """
    Useful for searching the web for current information, news, or anything not in internal documents.
    Input should be a search query.
    """
    # Live quotes and live weather have dedicated tools, and a search engine
    # answers those questions with whatever a page happened to say when it was
    # crawled. Asked for the NVIDIA price it returned a Robinhood page quoting
    # $202.55 "as of 2026-07-18" - a month stale - which then sat in the
    # findings next to the $214.72 the quote tool had just fetched, and the
    # division was done against the wrong one. Refuse the query instead of
    # returning a number that looks equally authoritative.
    owner = _tool_owned_query(query)
    if owner:
        return (
            f"This question is answered by the {owner} tool, not by web search - "
            "a crawled page carries whatever value it held when it was indexed, "
            "which may be weeks stale. Do not search for it. Say in your reply "
            f"that {owner} owns this figure so a later step can fetch it."
        )

    try:
        result = search_tool.invoke({"query": query})
        if result and result.get("results"):
            return _digest_search_results(result["results"])
        raise ValueError("Tavily returned no results")
    except Exception as e:
        try:
            return ddg.run(query)[:SEARCH_RESULTS_KEPT * SEARCH_SNIPPET_CHARS]
        except Exception as e2:
            return f"Error: Tavily failed ({str(e)}), DuckDuckGo fallback also failed ({str(e2)})"


@tool
def get_stock_price(symbol: str) -> dict:
    """
    Get stock price for a given symbol. (e.g 'MSFT', 'AAPL', 'GOOGL')
    using Alpha Vantage with API key in the URL.
    """
    api_key = os.environ.get("ALPHAVANTAGE_API_KEY")
    if not api_key:
        return {"error": "ALPHAVANTAGE_API_KEY environment variable is not set."}

    url = "https://www.alphavantage.co/query"
    params = {"function": "GLOBAL_QUOTE", "symbol": symbol, "apikey": api_key}
    try:
        # Without a timeout a slow Alpha Vantage hangs the whole turn forever.
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
    except requests.exceptions.RequestException as e:
        return {"error": f"Alpha Vantage request failed: {e}"}

    data = response.json()

    # GLOBAL_QUOTE returns the last CLOSE, not a live tick, and nothing in the
    # payload says so. A Monday-morning question got Friday's close and the
    # model reported it as "the current price". Label the age so it cannot.
    quote = data.get("Global Quote") or {}
    day = quote.get("07. latest trading day")
    if day:
        try:
            age = (datetime.date.today() - datetime.date.fromisoformat(day)).days
        except ValueError:
            age = 0
        if age >= 1:
            quote["freshness"] = (
                f"CLOSING price from {day}, {age} day(s) old. NOT a live price - "
                "describe it as the last close, never as the current price."
            )
    return data


# Dummy Tool
@tool
def purchase_stock(symbol: str, quantity: int) -> dict:
    """
    Simulate purchasing a given quantity of a stock symbol.
    
    HUMAN-IN-THE-LOOP:
    before confirming the purchase, this tool will interrupt
    and wait for a human decision ("yes" /anything else).
    """
    decision =  interrupt(f"Approve buying {quantity} shares of {symbol}?yes/no")

    if isinstance(decision,str) and decision.lower() == "yes":
        return {"message": f"Approved! You bought {quantity} shares of {symbol}."}
    else:
        return {"message": "Not approved"}


# The `wikipedia` package (1.4.0, unmaintained since 2014) defaults to a plain
# HTTP endpoint that 301-redirects on every call, a shared User-Agent that
# Wikimedia throttles, and no rate limiting. When Wikimedia answers with an HTML
# error page instead of JSON, the library calls r.json() on it anyway and raises
# "Expecting value: line 1 column 1 (char 0)".
import wikipedia as _wikipedia_lib

_wikipedia_lib.set_lang("en")
_wikipedia_lib.API_URL = "https://en.wikipedia.org/w/api.php"  # after set_lang, which resets it
_wikipedia_lib.set_user_agent("agentic-chatbot/1.0 (personal learning project)")
_wikipedia_lib.set_rate_limiting(True)

wiki = WikipediaAPIWrapper(top_k_results=1, doc_content_chars_max=1500)
@tool
def wikipedia(query: str) -> str:
    """Search Wikipedia for general knowledge, facts, people, places, or historical events. Not for internal engineering specs or company documents."""
    for attempt in range(2):
        try:
            result = wiki.run(query)
            if result:
                return result
            return f"No Wikipedia article matched '{query}'."
        except ValueError as e:
            # JSONDecodeError subclasses ValueError: Wikipedia replied with
            # something that was not JSON, so this is a transport problem
            # rather than a missing article. Worth one retry.
            if attempt == 0:
                continue
            return f"Wikipedia did not return a valid response for '{query}' ({e}). Try the web search tool."
        except Exception as e:
            return f"Wikipedia lookup for '{query}' failed: {e}"

@tool
def inspect_table(file_path: str, sheet_name: str = None) -> str:
    """
    Inspect the raw structure of a spreadsheet or CSV before extracting data —
    merged cells, sheet names, and the first 15 raw rows exactly as stored (no
    header assumptions). Handles .xlsx/.xlsm/.xls and .csv/.tsv. Use this FIRST
    on any unfamiliar or dense file, before calling run_pandas_code.
    """
    file_path = cloud_store.restore_path(file_path)
    extension = os.path.splitext(file_path)[1].lower()

    # The upload widget accepts CSV, so the first tool the model reaches for
    # must not choke on one - openpyxl cannot open a CSV at all.
    if extension in (".csv", ".tsv", ".txt"):
        try:
            separator = "\t" if extension == ".tsv" else ","
            head = pd.read_csv(
                file_path, sep=separator, header=None, nrows=15,
                dtype=str, keep_default_na=False,
            )
            file_size_mb = os.path.getsize(file_path) / (1024 * 1024)
            if file_size_mb > 50:
                total_rows = "Large file (row count skipped)"
            else:
                with open(file_path, encoding="utf-8", errors="replace") as f:
                    total_rows = sum(1 for _ in f)
        except Exception as e:
            return f"Error inspecting '{file_path}': {str(e)}"

        raw_rows = [
            "Row %d: %s" % (i + 1, tuple(row))
            for i, row in enumerate(head.itertuples(index=False, name=None))
        ]
        return (
            f"Delimited text file ({extension}), {total_rows} rows x "
            f"{head.shape[1]} cols, separator {separator!r}\n\n"
            "Raw rows (no header parsing):\n" + "\n".join(raw_rows)
        )

    # openpyxl reads OOXML only; a real legacy .xls is BIFF/OLE2 and needs xlrd,
    # which pandas drives. The upload widget accepts .xls, so this branch is the
    # difference between reading the file and apologising for it.
    if extension == ".xls":
        try:
            book = pd.ExcelFile(file_path)
            sheet = sheet_name if sheet_name in book.sheet_names else book.sheet_names[0]
            frame = pd.read_excel(book, sheet_name=sheet, header=None, dtype=str)
        except Exception as e:
            return f"Error inspecting '{file_path}': {str(e)}"

        raw_rows = [
            "Row %d: %s" % (i + 1, tuple(row))
            for i, row in enumerate(frame.head(15).itertuples(index=False, name=None))
        ]
        return (
            f"Sheets: {book.sheet_names}\n"
            f"Active sheet: {sheet} ({frame.shape[0]} rows x {frame.shape[1]} cols)\n"
            "Merged cell ranges: not reported for legacy .xls\n\n"
            "Raw rows (no header parsing):\n" + "\n".join(raw_rows)
        )

    try:
        wb = openpyxl.load_workbook(file_path, data_only=True)
        sheet = sheet_name if sheet_name else wb.sheetnames[0]
        ws = wb[sheet]

        merged = [str(r) for r in ws.merged_cells.ranges][:20]
        raw_rows = []
        for i, row in enumerate(ws.iter_rows(min_row=1, max_row=15, values_only=True)):
            raw_rows.append(f"Row {i+1}: {row}")

        return (
            f"Sheets: {wb.sheetnames}\n"
            f"Active sheet: {sheet} ({ws.max_row} rows x {ws.max_column} cols)\n"
            f"Merged cell ranges (first 20): {merged}\n\n"
            f"Raw rows (no header parsing):\n" + "\n".join(raw_rows)
        )
    except Exception as e:
        return f"Error inspecting '{file_path}': {str(e)}"


# Anything at or under this size is small enough to be an *answer*, so it goes
# to the model. Bigger results are bulk extractions: they are written to disk and
# rendered by the UI, and the model only gets a receipt. This keeps a 500-row
# sheet from being re-tokenised into a free-tier context window.
INLINE_ROW_LIMIT = 25
INLINE_CHAR_LIMIT = 2500
PREVIEW_ROWS = 3

EXTRACT_DIR = str(cloud_store.DATA_ROOT / "extracts")
EXTRACT_MARKER = "[EXTRACT:%s]"


def save_extract(df) -> str:
    """Write a DataFrame to disk and return the short id used to fetch it."""
    os.makedirs(EXTRACT_DIR, exist_ok=True)
    extract_id = uuid.uuid4().hex[:8]
    df.to_csv(os.path.join(EXTRACT_DIR, extract_id + ".csv"), index=False)
    cloud_store.persist_path(os.path.join(EXTRACT_DIR, extract_id + ".csv"))
    return extract_id


def load_extract(extract_id):
    """Read back a stored extraction, or None if it is gone."""
    path = os.path.join(EXTRACT_DIR, extract_id + ".csv")
    cloud_store.restore_path(path)
    if not os.path.exists(path):
        return None
    return pd.read_csv(path)


def extract_id_in(text):
    """Pull an extraction id out of a tool result, if it carries one."""
    match = re.search(r"\[EXTRACT:([0-9a-f]{8})\]", str(text))
    return match.group(1) if match else None

# Generated extraction code reaches for `import pandas as pd` by reflex, so a
# bare builtins dict (no __import__) makes the model burn turns retrying. Allow
# imports, but only of data libraries - not os, sys, subprocess or importlib.
_IMPORTABLE = {
    "pandas", "numpy", "openpyxl", "math", "statistics",
    "datetime", "re", "json", "collections", "itertools",
}


def _guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
    if name.split(".")[0] not in _IMPORTABLE:
        raise ImportError(
            f"'{name}' cannot be imported here. `pd`, `openpyxl` and `path` are already available."
        )
    return __import__(name, globals, locals, fromlist, level)


_SAFE_BUILTINS = {
    "abs": abs, "all": all, "any": any, "bool": bool, "dict": dict,
    "enumerate": enumerate, "filter": filter, "float": float, "getattr": getattr,
    "hasattr": hasattr, "int": int, "isinstance": isinstance, "len": len,
    "list": list, "map": map, "max": max, "min": min, "print": print,
    "range": range, "repr": repr, "reversed": reversed, "round": round,
    "set": set, "slice": slice, "sorted": sorted, "str": str, "sum": sum,
    "tuple": tuple, "type": type, "zip": zip,
    "__import__": _guarded_import,
}


@tool
def run_pandas_code(code: str, file_path: str = "") -> str:
    """
    Run Python to extract spreadsheet data or to compute any calculation.

    With a file: pass file_path after inspecting the sheet with inspect_table.
    The path is pre-loaded as `path`, with `pd` (pandas) and `openpyxl` already
    imported. Write code building a DataFrame called `result` - handling
    multi-row headers, skipping notes rows, or merging split columns.

    Without a file: omit file_path and assign the answer to `result`, e.g.
    `result = 2400 * 0.15`. `math` and `statistics` are importable.

    Only `result` is returned. Imports are limited to data libraries.
    """
    # One namespace, used as globals. Passing separate globals/locals dicts makes
    # exec'd code behave like a class body: comprehensions, lambdas and function
    # bodies open a new scope that skips the locals mapping, so `pd` and `path`
    # would raise NameError inside them.
    env = {
        "__builtins__": _SAFE_BUILTINS,
        "path": cloud_store.restore_path(file_path),
        "pd": pd,
        "openpyxl": openpyxl,
    }

    try:
        exec(code, env)
    except Exception as e:
        return f"Error executing extraction code: {str(e)}"

    result = env.get("result")
    if result is None:
        return "Error: code did not produce a variable named `result`."

    if not hasattr(result, "to_markdown"):
        return str(result)

    row_count = len(result)
    column_count = result.shape[1] if hasattr(result, "shape") and len(result.shape) > 1 else 1
    table = result.to_markdown(index=False)

    # Small enough to be the answer itself - the model needs to read it.
    if row_count <= INLINE_ROW_LIMIT and len(table) <= INLINE_CHAR_LIMIT:
        return table

    # Bulk extraction: hand the table to the UI, give the model a receipt only.
    extract_id = save_extract(result)
    columns = " | ".join(str(c) for c in result.columns)
    preview = result.head(PREVIEW_ROWS).to_markdown(index=False)

    return (
        f"{EXTRACT_MARKER % extract_id} Extracted {row_count} rows x {column_count} columns. "
        "The full table is already displayed to the user as a scrollable table with a "
        "download button.\n"
        f"Columns: {columns}\n"
        f"First {PREVIEW_ROWS} rows:\n{preview}\n\n"
        "Do NOT reproduce the table in your reply - the user can already see it. "
        "Confirm what was extracted in one or two sentences. If the user then asks "
        "something specific about the data, call run_pandas_code again with code that "
        "filters or aggregates down to just the rows needed to answer."
    )


@tool
def get_weather(city: str, units: str = "metric") -> str:
    """
    Fetch current weather conditions for a given city using the OpenWeather API.
 
    Args:
        city: Name of the city (e.g. "Lahore", "Karachi,PK", "London,GB").
              Adding a country code after a comma improves match accuracy.
        units: Unit system - "metric" (Celsius), "imperial" (Fahrenheit),
               or "standard" (Kelvin). Defaults to "metric".
 
    Returns:
        A human-readable summary of current weather conditions, or an
        error message if the lookup fails.
    """
    api_key = os.environ.get("OPENWEATHER_API_KEY")
    if not api_key:
        return "Error: OPENWEATHER_API_KEY environment variable is not set."
 
    url = "https://api.openweathermap.org/data/2.5/weather"
    params = {"q": city, "appid": api_key, "units": units}
 
    try:
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
    except requests.exceptions.HTTPError:
        status = response.status_code
        if status == 404:
            return f"Error: City '{city}' not found."
        elif status == 401:
            return "Error: Invalid OpenWeather API key."
        else:
            return f"Error: OpenWeather API returned status {status}."
    except requests.exceptions.RequestException as e:
        return f"Error: Failed to reach OpenWeather API - {e}"
 
    data = response.json()
    unit_symbol = {"metric": "°C", "imperial": "°F", "standard": "K"}.get(units, "")
 
    description = data["weather"][0]["description"].capitalize()
    temp = data["main"]["temp"]
    feels_like = data["main"]["feels_like"]
    humidity = data["main"]["humidity"]
    wind_speed = data["wind"]["speed"]
    location_name = data.get("name", city)
    country = data.get("sys", {}).get("country", "")
 
    return (
        f"Weather in {location_name}, {country}: {description}. "
        f"Temperature: {temp}{unit_symbol} (feels like {feels_like}{unit_symbol}). "
        f"Humidity: {humidity}%. Wind speed: {wind_speed} m/s."
    )

# ---------------------------------------------------------------- clock

@tool
def get_current_datetime(timezone: str = "") -> str:
    """
    Today's date and the current time. Call this FIRST for anything that depends
    on "now" - this week, today, latest, current, upcoming, how old, how long
    until. Training data has a cutoff and there is no clock without this tool.

    Args:
        timezone: IANA name such as "Asia/Karachi", "Europe/London" or "UTC".
                  Defaults to the server's own timezone.
    """
    if timezone:
        try:
            now = datetime.datetime.now(ZoneInfo(timezone))
        except Exception:
            return (
                f"Unknown timezone '{timezone}'. Use an IANA name "
                "such as 'Asia/Karachi', 'UTC' or 'America/New_York'."
            )
    else:
        now = datetime.datetime.now().astimezone()

    offset = now.strftime("%z")
    return (
        f"{now.strftime('%A, %d %B %Y')} at {now.strftime('%H:%M')} "
        f"({now.tzname()}, UTC{offset[:3]}:{offset[3:]}). "
        f"ISO 8601: {now.isoformat(timespec='seconds')}."
    )


# ------------------------------------------------------------ web pages

_FETCH_CHAR_LIMIT = 6000
_FETCH_BYTE_LIMIT = 2 * 1024 * 1024
_FETCH_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; agentic-chatbot/1.0; personal learning project)"
}


def _url_is_safe(url):
    """Reject anything that is not a public http(s) address.

    The URL reaches this tool from model output, which can be steered by the
    contents of a page or an uploaded document. Without this check that is a
    request forgery primitive pointed at localhost and the local network.
    """
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return False, "Only http:// and https:// URLs can be fetched."

    try:
        addresses = socket.getaddrinfo(parsed.hostname, None)
    except Exception:
        return False, f"Could not resolve '{parsed.hostname}'."

    for address in addresses:
        ip = ipaddress.ip_address(address[4][0])
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast):
            return False, "Refusing to fetch a private or loopback address."

    return True, ""


@tool
def fetch_url(url: str) -> str:
    """
    Read one specific web page and return its text. Use this when the user gives
    a URL, or when a search result has to be opened to answer properly. Use
    web_search instead when there is no URL yet and a page still has to be found.
    """
    safe, reason = _url_is_safe(url)
    if not safe:
        return f"Error: {reason}"

    try:
        response = requests.get(url, headers=_FETCH_HEADERS, timeout=15, stream=True)
        response.raise_for_status()
        content_type = response.headers.get("Content-Type", "").lower()

        if "pdf" in content_type:
            return (
                "That URL is a PDF. Ask the user to upload it so it can be indexed "
                "and searched with retriever_tool."
            )
        if not any(kind in content_type for kind in ("html", "text", "json", "xml")):
            return f"Error: unsupported content type '{content_type}'."

        body = response.raw.read(_FETCH_BYTE_LIMIT, decode_content=True)
    except requests.exceptions.RequestException as e:
        return f"Error fetching '{url}': {e}"

    text = body.decode(response.encoding or "utf-8", errors="replace")

    if "html" in content_type:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(text, "lxml")
        for element in soup(["script", "style", "noscript", "nav", "footer",
                             "header", "form"]):
            element.decompose()
        title = soup.title.get_text(strip=True) if soup.title else url
        lines = [line.strip() for line in soup.get_text("\n").splitlines() if line.strip()]
        text = "# " + title + "\n\n" + "\n".join(lines)

    if len(text) > _FETCH_CHAR_LIMIT:
        text = text[:_FETCH_CHAR_LIMIT] + (
            "\n\n[Page truncated. Fetch a more specific URL if the answer is "
            "further down.]"
        )
    return text or "The page returned no readable text."


# --------------------------------------------------------------- images

# OCR runs locally on purpose. Handing the image itself to a vision model costs
# visual tokens on every turn it stays in history, and the Groq free tier only
# allows 8000 tokens per minute - extracting the text here means the model pays
# for the ~170 tokens of prose instead of the picture.
#
# The engine is RapidOCR (PP-OCRv6 on onnxruntime) rather than easyocr, which
# this used to be. Measured on uploads/IMG-20260303-WA0001.jpg, easyocr read
# "PLC/DCS/SCADA" as "PLC/DCSISCADA", "gt=0" as "gt-0" and every sentence-final
# period as "_" or ":". Wrong text is the expensive failure: the model answers
# from it, the user asks again, and the retry costs more tokens than the whole
# extraction did.

def _rows_from_boxes(boxes, texts):
    """Join detections sharing a visual line into one line of text.

    RapidOCR detects text clusters, not lines, so one line of a code screenshot
    arrives as several entries - '"*', 'ts', '"${capture}.js"'. Joined with
    newlines that reads as three truncated lines, which is what made a settings
    file come back looking corrupted. Every box carries its coordinates, so the
    original rows can simply be rebuilt.

    Tolerance comes from the median box height rather than a fixed pixel count,
    so it holds for a phone photo and a 4K screenshot alike. Rows are compared
    against the first box in the row: an accumulating mean would let a column of
    slightly drifting boxes merge two real lines into one.
    """
    import numpy as np

    texts = list(texts)
    if boxes is None or len(boxes) != len(texts):
        return texts

    boxes = np.asarray(boxes, dtype=float)
    centers = boxes[:, :, 1].mean(axis=1)
    heights = boxes[:, :, 1].max(axis=1) - boxes[:, :, 1].min(axis=1)
    lefts = boxes[:, :, 0].min(axis=1)
    tolerance = float(np.median(heights)) * 0.6

    rows = []
    for index in sorted(range(len(texts)), key=lambda i: centers[i]):
        if rows and abs(centers[index] - centers[rows[-1][0]]) <= tolerance:
            rows[-1].append(index)
        else:
            rows.append([index])

    lines = []
    for row in rows:
        # Left-to-right within the row, or the tokens come back shuffled.
        row.sort(key=lambda i: lefts[i])
        line = " ".join(texts[i].strip() for i in row if texts[i].strip())
        if line:
            lines.append(line)
    return lines


# A gutter number can land on its own line or glued to the code beside it, so
# both shapes have to be recognised from the same pass.
_GUTTER_PREFIX = re.compile(r"^(\d{1,4})\s+(?=\S)")


def _strip_line_numbers(lines):
    """Drop editor gutter numbers from an OCR'd code screenshot.

    OCR reads the gutter as content, so a requirements.txt screenshot arrives as
    "# Core LLM", "2", "langchain>=0.2.0", "3 langchain-core>=0.2.0", "5", ... .
    The recognition is correct - the digits really are in the image - but the
    result reads as corrupted text, and a small model responds to it by deciding
    the tool failed rather than by answering from it.

    A rising run is the whole signal. A genuine column of numbers in a
    spreadsheet screenshot is not reliably ascending by one, and a single stray
    digit is not a gutter, so both survive - only a real gutter clears the bar.
    """
    leading = []
    for line in lines:
        if line.isdigit():
            leading.append(int(line))
        else:
            match = _GUTTER_PREFIX.match(line)
            leading.append(int(match.group(1)) if match else None)

    numbers = [value for value in leading if value is not None]
    if len(numbers) < 3:
        return lines

    # OCR drops or misreads the odd gutter digit, so demand a strong trend
    # rather than a perfect sequence.
    rising = sum(1 for previous, current in zip(numbers, numbers[1:]) if current > previous)
    if rising / (len(numbers) - 1) < 0.8:
        return lines

    kept = []
    for line, value in zip(lines, leading):
        if value is None:
            kept.append(line)
        elif line.isdigit():
            continue
        else:
            kept.append(_GUTTER_PREFIX.sub("", line, count=1))
    return kept


_ocr_engine = None


def get_ocr_engine():
    """Build the OCR engine once per process and keep it.

    rapidocr is imported inside the function, not at module scope, for the same
    reason the embeddings are: the UI imports this module before
    it can draw anything, and onnxruntime costs ~1.4s to import. A text-only
    conversation should never pay that.
    """
    global _ocr_engine
    if _ocr_engine is None:
        from rapidocr import RapidOCR

        # Model weights ship inside the wheel, so unlike the previous engine
        # this does not reach the network on first call.
        params = {"Global.model_root_dir": str(cloud_store.DATA_ROOT / "ocr_models")} if cloud_store.ENABLED else None
        _ocr_engine = RapidOCR(params=params)
    return _ocr_engine


@tool
def extract_text_from_image(file_path: str) -> str:
    """Extract text from an image file (png/jpg) using OCR.

    Use this when the user uploads an image and asks to read or extract text
    from it.
    """
    # The path arrives as model output echoing the frontend's "Attached file(s)"
    # note, so it can be hallucinated or stale. A plain sentence beats letting
    # the engine raise, which would surface as an opaque cv2 decode error.
    file_path = cloud_store.restore_path(file_path)
    if not os.path.exists(file_path):
        return "No file at %s. Ask the user to attach the image again." % file_path

    try:
        result = get_ocr_engine()(file_path)
    except Exception as e:
        return "Error extracting text from image: %s" % e

    # RapidOCR returns txts=None for an image it found no text in, rather than
    # an empty tuple, so the None check cannot be folded into the join below.
    # Rebuild visual rows before anything else: the gutter number then reliably
    # arrives as a leading "12 " on its own row, which is the shape
    # _strip_line_numbers already recognises.
    lines = _rows_from_boxes(result.boxes, result.txts or ())
    lines = _strip_line_numbers(lines)
    if not lines:
        return "No text found in the image."

    # The preamble is not decoration. Returned bare, the extracted text is just
    # a blob - and a blob that happens to look like a config file reads as a
    # failure to a small model. qwen3.6-27b did exactly that: given a clean
    # extraction of a requirements.txt screenshot it reasoned "The OCR tool
    # failed... Probably cannot read" and apologised to the user, while the
    # tool result sat in its context with status success. Stating the outcome
    # and the character count leaves nothing to infer.
    text = "\n".join(lines)
    return "OCR succeeded - %d characters read from the image:\n\n%s" % (len(text), text)

# --------------------------------------------------------------- charts

CHART_MARKER = "[CHART:%s]"
_CHART_KINDS = ("line", "bar", "area", "scatter")


def save_chart(spec):
    """Write a chart spec next to its extraction and return its short id."""
    os.makedirs(EXTRACT_DIR, exist_ok=True)
    chart_id = uuid.uuid4().hex[:8]
    path = os.path.join(EXTRACT_DIR, "chart_" + chart_id + ".json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(spec, f)
    cloud_store.persist_path(path)
    return chart_id


def load_chart(chart_id):
    path = os.path.join(EXTRACT_DIR, "chart_" + chart_id + ".json")
    cloud_store.restore_path(path)
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def chart_id_in(text):
    """Pull a chart id out of a tool result, if it carries one."""
    match = re.search(r"\[CHART:([0-9a-f]{8})\]", str(text))
    return match.group(1) if match else None


@tool
def plot_data(extract_id: str, kind: str, x: str, y: str = "") -> str:
    """
    Draw a chart from a stored extraction. Only works on data a previous tool
    saved and reported with an id like [EXTRACT:1a2b3c4d] - pass that id here.
    The chart is rendered for the user; do not describe its shape unless asked.

    Args:
        extract_id: the 8-character id from the extraction receipt.
        kind: one of line, bar, area, scatter.
        x: column name for the horizontal axis.
        y: column to plot, or several separated by commas. Defaults to every
           remaining numeric column.
    """
    kind = (kind or "").strip().lower()
    if kind not in _CHART_KINDS:
        return f"Error: kind must be one of {', '.join(_CHART_KINDS)}."

    df = load_extract(extract_id)
    if df is None:
        return f"Error: no stored extraction with id '{extract_id}'."

    columns = [str(name) for name in df.columns]
    if x not in columns:
        return f"Error: column '{x}' is not in this extraction. Columns: {', '.join(columns)}."

    if y:
        y_columns = [name.strip() for name in y.split(",") if name.strip()]
        missing = [name for name in y_columns if name not in columns]
        if missing:
            return (
                f"Error: column(s) {', '.join(missing)} are not in this extraction. "
                f"Columns: {', '.join(columns)}."
            )
    else:
        y_columns = [
            name for name in columns
            if name != x and pd.api.types.is_numeric_dtype(df[name])
        ]
        if not y_columns:
            return "Error: no numeric column to plot. Name one explicitly with `y`."

    chart_id = save_chart({
        "extract_id": extract_id, "kind": kind, "x": x, "y": y_columns,
    })
    return (
        f"{CHART_MARKER % chart_id} Drew a {kind} chart of "
        f"{', '.join(y_columns)} against {x}. It is already on screen for the "
        "user - confirm in one sentence, do not restate the numbers."
    )


# --------------------------------------------------------------- memory

def _ensure_memory_table():
    if cloud_store.ENABLED:
        return
    connection.execute(
        "CREATE TABLE IF NOT EXISTS memories ("
        "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  fact TEXT NOT NULL,"
        "  tag TEXT,"
        "  created_at TEXT NOT NULL)"
    )
    connection.commit()


_MEMORY_STOPWORDS = {
    "the", "a", "an", "is", "are", "was", "were", "of", "to", "in", "on", "for",
    "and", "or", "my", "me", "i", "what", "whats", "do", "does", "did", "you",
}


def _memory_query(sql, params=()):
    if cloud_store.ENABLED:
        return cloud_store.memory_query(sql, params)
    cursor = connection.execute(sql, params)
    rows = cursor.fetchall() if cursor.description else []
    connection.commit()
    return rows


def _memory_tokens(text):
    # Trailing -s is trimmed so "where I live" matches "user lives in Lahore".
    # ponytail: crude stemming, swap in a real stemmer if matching gets fussy.
    return {
        word[:-1] if len(word) > 3 and word.endswith("s") else word
        for word in re.findall(r"[a-z0-9]+", str(text).lower())
        if word not in _MEMORY_STOPWORDS and len(word) > 1
    }


@tool
def remember(fact: str, tag: str = "") -> str:
    """
    Call this the moment the user tells you something about themselves worth keeping: their name, where they live, what they work on, a preference, or anything prefixed with "remember".
    Each conversation has its own separate history, so this is the only thing
    that carries a fact from one chat to the next. Save only what the user says
    about themselves, never page or document contents.

    Args:
        fact: a single self-contained sentence, e.g. "Zeeshan is building an
              agentic chatbot with LangGraph and Chainlit".
        tag: optional one-word category such as "preference" or "work".
    """
    fact = (fact or "").strip()
    if not fact:
        return "Error: nothing to remember."

    existing = _memory_query("SELECT fact FROM memories")
    if any(row[0].strip().lower() == fact.lower() for row in existing):
        return "Already remembered that."

    _memory_query(
        "INSERT INTO memories (fact, tag, created_at) VALUES (?, ?, ?)",
        (fact, tag.strip() or None,
         datetime.datetime.now().isoformat(timespec="seconds")),
    )
    return f"Remembered: {fact}"


@tool
def recall(query: str, limit: int = 5) -> str:
    """
    Call this before answering anything personal about the user - what they like, what they do, what they are called, what they told you before.
    It returns facts they gave you in earlier conversations. This chat's history
    covers only this chat, so without this tool you know nothing about them.
    """
    rows = _memory_query(
        "SELECT fact, tag, created_at FROM memories ORDER BY id DESC"
    )
    if not rows:
        return "Nothing has been remembered yet."

    wanted = _memory_tokens(query)
    scored = [
        (len(wanted & _memory_tokens(fact + " " + (tag or ""))), fact, tag, created_at)
        for fact, tag, created_at in rows
    ]
    hits = [row for row in scored if row[0]]
    # No keyword overlap at all - the most recent facts still beat nothing.
    scored = hits or scored
    scored.sort(key=lambda row: row[0], reverse=True)

    lines = [
        "- %s%s (saved %s)" % (fact, f" [{tag}]" if tag else "", created_at[:10])
        for _, fact, tag, created_at in scored[:max(1, min(limit, 20))]
    ]
    return "Remembered about this user:\n" + "\n".join(lines)



@tool
def forget(query: str) -> str:
    """
    Call this when the user asks to delete, forget, erase or take back something you remembered.
    Pass what they want gone, or "everything" to clear all of it. Without this
    tool a saved fact is permanent, so never tell the user you cannot delete one.
    """
    rows = _memory_query("SELECT id, fact FROM memories")
    if not rows:
        return "Nothing is remembered, so there is nothing to forget."

    if query.strip().lower() in ("everything", "all", "all of it", "everything you know"):
        targets = rows
    else:
        wanted = _memory_tokens(query)
        scored = [
            (len(wanted & _memory_tokens(fact)), row_id, fact)
            for row_id, fact in rows
        ]
        best = max(score for score, _, _ in scored)
        if not best:
            # "delete that" carries no keywords: unambiguous with one fact
            # stored, a guess worth refusing with several.
            if len(rows) == 1:
                targets = rows
            else:
                return (
                    "Which one? Currently remembered: "
                    + "; ".join(fact for _, fact in rows)
                )
        else:
            targets = [(row_id, fact) for score, row_id, fact in scored if score == best]

    for row_id, _ in targets:
        _memory_query("DELETE FROM memories WHERE id = ?", (row_id,))
    return "Forgot:\n" + "\n".join("- " + fact for _, fact in targets)


# -------------------------------------------------------- stock history

@tool
def get_stock_history(symbol: str, days: int = 30) -> str:
    """
    Daily open/high/low/close/volume history for a stock symbol, most recent
    first. Use this for trends or any question needing more than one price; use
    get_stock_price for a single current quote. The result can be charted with
    plot_data.

    Args:
        symbol: ticker such as "MSFT", "AAPL", "NVDA".
        days: how many trading days to return, up to 100.
    """
    api_key = os.environ.get("ALPHAVANTAGE_API_KEY")
    if not api_key:
        return "Error: ALPHAVANTAGE_API_KEY environment variable is not set."

    try:
        response = requests.get(
            "https://www.alphavantage.co/query",
            params={
                "function": "TIME_SERIES_DAILY", "symbol": symbol,
                "outputsize": "compact", "apikey": api_key,
            },
            timeout=15,
        )
        response.raise_for_status()
        payload = response.json()
    except requests.exceptions.RequestException as e:
        return f"Error: Alpha Vantage request failed: {e}"

    series = payload.get("Time Series (Daily)")
    if not series:
        # Alpha Vantage reports bad symbols and throttling in the body, not the
        # status code.
        detail = (payload.get("Note") or payload.get("Information")
                  or payload.get("Error Message") or "")
        return f"Error: no daily history for '{symbol}'. {detail}".strip()

    frame = pd.DataFrame([
        {
            "date": day,
            "open": float(values["1. open"]),
            "high": float(values["2. high"]),
            "low": float(values["3. low"]),
            "close": float(values["4. close"]),
            "volume": int(values["5. volume"]),
        }
        for day, values in sorted(series.items(), reverse=True)
    ]).head(max(1, min(days, 100)))

    table = frame.to_markdown(index=False)
    extract_id = save_extract(frame)

    if len(frame) <= INLINE_ROW_LIMIT and len(table) <= INLINE_CHAR_LIMIT:
        return (
            f"{EXTRACT_MARKER % extract_id} {symbol.upper()} daily history, most "
            f"recent first. To chart it, call plot_data with extract_id "
            f"'{extract_id}', x='date' and the column asked for.\n{table}"
        )

    # Too big to be the answer itself: the UI shows the table, the model gets a
    # receipt plus the id it can hand straight to plot_data.
    return (
        f"{EXTRACT_MARKER % extract_id} {symbol.upper()} daily history, "
        f"{len(frame)} rows, most recent first. The table is already displayed to "
        "the user. Columns: date | open | high | low | close | volume. "
        f"Latest close: {frame.iloc[0]['close']} on {frame.iloc[0]['date']}. "
        "Do NOT reproduce the table. To chart it, call plot_data with this "
        "extract id, x='date' and the column asked for."
    )


# -------------------------------------------------------- Cursor cloud agent (demo)

def _normalize_github_repo(repo: str) -> str:
    """Accept org/repo or a full GitHub URL; return https://github.com/..."""
    repo = (repo or "").strip().rstrip("/")
    if not repo:
        return ""
    if repo.startswith("http://") or repo.startswith("https://"):
        return repo
    return "https://github.com/%s" % repo.lstrip("/")


@tool
def cursor_coding_agent(task_description: str, github_repo: str = "") -> str:
    """
    Run a Cursor Cloud Agent against a GitHub repository for coding or
    debugging. Demo-oriented: uses composer-2.5 and consumes Cursor Pro usage.

    Args:
        task_description: Exact instructions for the coding or debugging task.
        github_repo: GitHub URL or org/repo. Defaults to CURSOR_DEMO_REPO env.
    """
    api_key = (os.environ.get("CURSOR_API_KEY") or "").strip()
    if not api_key:
        return (
            "Error: CURSOR_API_KEY is not set. Add a Cursor user API key from "
            "https://cursor.com/dashboard/integrations to .env for this demo."
        )

    repo = _normalize_github_repo(
        github_repo or os.environ.get("CURSOR_DEMO_REPO", "")
    )
    if not repo:
        return (
            "Error: No repository specified. Pass github_repo (org/repo or URL) "
            "or set CURSOR_DEMO_REPO in the environment."
        )

    auto_pr = (os.environ.get("CURSOR_AUTO_CREATE_PR") or "").strip().lower() in {
        "1", "true", "yes", "on",
    }

    try:
        from cursor_sdk import (
            Agent,
            AgentOptions,
            CloudAgentOptions,
            CloudRepository,
            CursorAgentError,
        )
    except ImportError:
        return "Error: cursor-sdk is not installed. Run: pip install cursor-sdk"

    try:
        result = Agent.prompt(
            task_description,
            AgentOptions(
                api_key=api_key,
                model="composer-2.5",
                cloud=CloudAgentOptions(
                    repos=[CloudRepository(url=repo)],
                    auto_create_pr=auto_pr,
                    skip_reviewer_request=True,
                ),
            ),
        )
    except CursorAgentError as err:
        return "Error: Cursor agent failed to start: %s" % getattr(
            err, "message", err
        )
    except Exception as err:
        return "Error: Cursor agent failed: %s" % err

    status = getattr(result, "status", "unknown")
    text = (getattr(result, "result", None) or "").strip()
    agent_id = getattr(result, "agent_id", None) or ""
    git = getattr(result, "git", None)

    lines = [
        "Cursor cloud agent finished (status=%s)." % status,
        "Repository: %s" % repo,
    ]
    if agent_id:
        lines.append("Agent id: %s" % agent_id)
    if git is not None:
        for attr in ("branch_name", "pr_url", "url", "branch"):
            value = getattr(git, attr, None)
            if value:
                lines.append("%s: %s" % (attr, value))
        git_text = str(git).strip()
        if git_text and git_text not in "\n".join(lines):
            lines.append("git: %s" % git_text[:500])
    if text:
        lines.append("")
        lines.append(text[:4000])
    if status == "error":
        return "Error: " + "\n".join(lines)
    return "\n".join(lines)


# web_search, not search_tool: the wrapper is what falls back to DuckDuckGo
# when Tavily rate-limits. Binding the raw tool left that fallback dead.
tools = [
    retriever_tool, web_search, fetch_url, wikipedia, extract_text_from_image,
    get_current_datetime,
    get_weather, get_stock_price, get_stock_history, purchase_stock,
    inspect_table, run_pandas_code, plot_data,
    remember, recall, forget,
    cursor_coding_agent,
    *GOOGLE_TOOLS,
]

DEFAULT_MODEL = "qwen/qwen3.6-27b"
current_model = DEFAULT_MODEL
current_provider = "groq"


def set_model(model_id: str, provider: str = "groq"):
    """Swap the chat model used by every agent node.

    Each agent node looks up llm as a module global at call time and binds its
    own tool subset, so reassigning llm here changes the model for every
    following turn without rebuilding the graph.

    Note: reasoning_effort="none" is only accepted by the qwen models; the
    gpt-oss models reject anything other than low/medium/high.
    """
    global llm, current_model, current_provider

    # Keyed on the pair, not the id alone: the same model name is served by more
    # than one provider here, and comparing ids only would silently keep the old
    # client when a user switched provider to escape a rate limit.
    if (model_id, provider) == (current_model, current_provider):
        return llm

    if provider == "google":
        from langchain_google_genai import ChatGoogleGenerativeAI
        llm = ChatGoogleGenerativeAI(model=model_id, temperature=0)
    elif provider == "openrouter":
        # OpenRouter speaks the OpenAI wire format, so the OpenAI client with a
        # different base_url is the whole integration. Its key is separate from
        # OPENAI_API_KEY and must be passed explicitly, or the client picks up
        # the OpenAI one and authenticates against the wrong service.
        from langchain_openai import ChatOpenAI
        llm = ChatOpenAI(
            model=model_id,
            temperature=0,
            base_url="https://openrouter.ai/api/v1",
            api_key=os.environ.get("OPENROUTER_API_KEY"),
        )
    elif provider == "openai":
        from langchain_openai import ChatOpenAI
        llm = ChatOpenAI(model=model_id, temperature=0)
    elif provider == "ollama":
        # ChatOpenAI against Ollama's OpenAI-compatible /v1 endpoint used to be
        # here, but that endpoint ignores the "think" field entirely - Qwen3's
        # thinking mode (on by default, "capabilities": ["...", "thinking"] in
        # `ollama list`) stayed on for every one of this graph's several LLM
        # calls per turn (supervisor, agent, synthesizer), each paying for a
        # full hidden reasoning trace. Measured locally: 17-21s per call with
        # thinking on vs 0.3-0.6s with it off via the native API's "think"
        # field - a ~30x difference, and the actual cause of "slow" here, not
        # a networking or config problem. ChatOllama (native client) exposes
        # that field directly as reasoning=False; ChatOpenAI has no equivalent.
        from langchain_ollama import ChatOllama
        base_url = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
        base_url = base_url.removesuffix("/v1")
        llm = ChatOllama(
            model=model_id,
            temperature=0,
            reasoning=False,
            base_url=base_url,
            keep_alive="30m",
        )
    elif model_id.startswith("qwen"):
        llm = ChatGroq(model=model_id, reasoning_effort="none", temperature=0, **GROQ_GENERATION_OPTIONS)
    elif "gpt-oss" in model_id:
        llm = ChatGroq(model=model_id, reasoning_effort="low", temperature=0, **GROQ_GENERATION_OPTIONS)
    else:
        llm = ChatGroq(model=model_id, temperature=0, **GROQ_GENERATION_OPTIONS)

    current_model = model_id
    current_provider = provider
    return llm


# Built from the actual tool objects. A hand-written list drifts out of date,
# and a prompt naming tools that do not exist makes the model distrust real
# tool results and ask the user to restate the question instead.
TOOL_GUIDE = "\n".join(
    "- `%s`: %s" % (t.name, (t.description or "").strip().splitlines()[0])
    for t in tools
)

# Every tool this build can actually run. Derived from `tools` for the same
# reason TOOL_GUIDE is: a hand-kept second list is one delete away from being
# wrong, and here being wrong means silently dropping live tool calls out of
# history. Used by _strip_unknown_tool_calls.
KNOWN_TOOL_NAMES = {t.name for t in tools}


# This was max_tokens=40 with token_counter=len, which counts *messages*, not
# tokens - so history was unbounded in size and one fat tool result could carry
# the whole request past the model's rate limit. Budget real tokens instead.
# Groq's free tier allows 8000 tokens per minute for the qwen models, and a turn
# makes several calls inside that minute (supervisor routing, then the agent).
MAX_HISTORY_TOKENS = 2500
SUPERVISOR_HISTORY_TOKENS = 600

# trim_messages cannot split a single message, and start_on="human" keeps a
# human message together with the tool traffic that follows it - so one oversized
# tool result sails past the budget untouched no matter how small the budget is.
# Clamp the results themselves; this covers every tool, including future ones.
MAX_TOOL_RESULT_CHARS = 4000


def _strip_unknown_tool_calls(messages):
    """Drop calls to tools that no longer exist from the replayed history.

    Removing a tool does not remove it from threads that already used it. The
    model reads its own past calls as a worked example and calls the tool again,
    and the provider rejects the whole turn: "attempted to call tool 'calculator'
    which was not in request.tools". That fails every following turn in the
    thread, not just one.

    Matched against every registered tool rather than the running agent's own
    subset - a Research_Agent call sitting in history is real context while the
    Data_Agent works, and must survive.

    A tool result whose call is gone would be an orphan the provider also
    rejects, so the ids are collected on the first pass and their ToolMessages
    dropped on the second.
    """
    orphaned_ids = set()
    kept = []

    for message in messages:
        calls = getattr(message, "tool_calls", None)
        if not calls:
            kept.append(message)
            continue

        live = [call for call in calls if call.get("name") in KNOWN_TOOL_NAMES]
        if len(live) == len(calls):
            kept.append(message)
            continue

        orphaned_ids.update(
            call.get("id") for call in calls if call.get("name") not in KNOWN_TOOL_NAMES
        )

        # An assistant turn that was nothing but a dead tool call carries no
        # meaning once the call is gone; keeping the empty shell just costs
        # tokens and invites the model to imitate the blank turn.
        if not live and not str(message.content).strip():
            continue

        # additional_kwargs carries the provider-native copy of the same calls.
        # Updating only .tool_calls leaves that copy to be re-serialised onto
        # the wire, so the dead call reaches the provider anyway.
        extra = dict(getattr(message, "additional_kwargs", {}) or {})
        if extra.get("tool_calls"):
            extra["tool_calls"] = [
                raw for raw in extra["tool_calls"]
                if (raw.get("function") or {}).get("name") in KNOWN_TOOL_NAMES
            ]

        kept.append(message.model_copy(update={
            "tool_calls": live,
            "additional_kwargs": extra,
        }))

    if not orphaned_ids:
        return kept

    return [
        message for message in kept
        if not (isinstance(message, ToolMessage) and message.tool_call_id in orphaned_ids)
    ]


def _clamp_tool_results(messages):
    clamped = []
    for message in _strip_unknown_tool_calls(messages):
        if isinstance(message, ToolMessage) and isinstance(message.content, str) \
                and len(message.content) > MAX_TOOL_RESULT_CHARS:
            clamped.append(message.model_copy(update={
                "content": message.content[:MAX_TOOL_RESULT_CHARS] + "\n...[truncated]",
            }))
        else:
            clamped.append(message)
    return clamped


# State
class Chatstate(TypedDict, total=False):
    messages: Annotated[list[BaseMessage], add_messages] # reducer is applied automatically to the messages list
    next: str
    sender: str
    plan: list[dict]      # [{agent, task, status, note}]
    plan_cursor: int      # index of the step currently running
    plan_for: str         # turn key the plan was built for
    replans: int
    step_attempts: int
    current_task: str     # rendered brief handed to the dispatched agent

# Tool groupings
data_tools = [run_pandas_code, inspect_table, plot_data]
research_tools = [web_search, fetch_url, wikipedia, retriever_tool, extract_text_from_image]
financial_tools = [get_stock_price, get_stock_history, purchase_stock, get_weather]
memory_tools = [remember, recall, forget, get_current_datetime]
coding_tools = [cursor_coding_agent]
google_tools = list(GOOGLE_TOOLS)

def make_agent(name: str, system_prompt: str, tools_list: list):
    prompt = SystemMessage(content=system_prompt + "\n\n" + (
        "After a tool returns, ANSWER FROM THAT RESULT. Never reply by asking the "
        "user to repeat or clarify something they already asked - the tool output is "
        "the answer to their question."
    ))
    
    def agent_node(state: Chatstate):
        current_llm_with_tools = llm.bind_tools(tools_list) if tools_list else llm
        clamped_history = _clamp_tool_results(state["messages"])
        recent = trim_messages(
            clamped_history,
            max_tokens=MAX_HISTORY_TOKENS,
            token_counter=count_tokens_approximately,
            strategy="last",
            start_on="human",
            include_system=False,
            allow_partial=False,
        )
        # A single tool result can be bigger than the whole budget, and
        # trim_messages then returns nothing at all - which drops the user's
        # question and makes the agent answer a blank prompt. Strict models
        # also reject a ToolMessage without its preceding AIMessage.
        if not recent:
            last_human = HumanMessage(content=_last_human_text(state["messages"]))
            if len(clamped_history) >= 2 and isinstance(clamped_history[-1], ToolMessage) and isinstance(clamped_history[-2], AIMessage):
                recent = [last_human, clamped_history[-2], clamped_history[-1]]
            else:
                recent = [last_human, clamped_history[-1]]
            
        # The supervisor's plan step, injected as a second system message so it
        # never enters message history and never gets trimmed away.
        head = [prompt]
        task = state.get("current_task")
        if task:
            head.append(SystemMessage(content="PLAN CONTEXT\n" + task))

        response = current_llm_with_tools.invoke([*head, *recent])
        return {"messages": [response], "sender": name}
    
    return agent_node

Data_Agent = make_agent(
    "Data_Agent",
    "You are the Data Analyst Agent. You handle spreadsheets, CSVs, charts, and math calculations. "
    "Extract data carefully and explain your findings. "
    # You are the only agent with a calculator. Answering from your own head
    # defeats the entire reason a step was routed here.
    "EVERY number you report must come out of a run_pandas_code call - every "
    "multiplication, division and comparison, including ones you are confident "
    "you can do in your head. Never state an arithmetic result you did not "
    "compute with the tool.",
    data_tools
)

_RESEARCH_SYSTEM = (
    "You are the Research Agent. You search the web, fetch URLs, query Wikipedia, and read internal RAG documents. "
    "Do not invent information. Always rely on your tools for external facts. "
    # Handed "find the capital, its area and its population" it also went and
    # searched for the temperature and the stock price - both of which had
    # their own later steps and their own dedicated tools - burning searches on
    # figures it was never asked for and returning staler ones than the tools.
    "Answer ONLY the step you were given. Other parts of the user's request have "
    "their own steps and their own specialists; do not research them here. "
    # Search results routinely disagree. Islamabad's area came back as 65, 906
    # and 2717 km2 in one digest and the agent picked 906 with no mention that
    # the other two existed, which hides the real uncertainty from every later
    # step and from the user.
    # First draft of this rule said only "give the range", and the agent started
    # handing the synthesizer two candidate temperatures and two candidate stock
    # prices with no verdict, which turned the final answer into a branching
    # what-if instead of an answer. Reporting the disagreement is useful;
    # refusing to resolve it just moves the decision to a node with less
    # evidence than this one.
    "ALWAYS COMMIT TO ONE FIGURE. When your sources disagree by more than about "
    "20%, name the number you are using, say in one clause why that source wins "
    "(more recent, primary, better matched to what was asked), and mention the "
    "others only as a brief aside. Never hand on a list of candidates. Prefer "
    "dated, primary sources over social posts and undated pages."
)
Research_Agent = make_agent(
    "Research_Agent",
    _RESEARCH_SYSTEM,
    research_tools
)

Financial_Agent = make_agent(
    "Financial_Agent",
    "You are the Financial Agent. You fetch stock prices, market history, handle purchases, and check weather. "
    "Provide accurate and concise market data. "
    # The quote carries its own age in a "freshness" field, but only the tool
    # result has it - later steps and the synthesizer see this agent's prose and
    # nothing else. Dropped from the prose, a three-day-old close was reported
    # as "the NVIDIA stock price is $214.72".
    "If a quote carries a 'freshness' field, repeat its warning in your own reply: "
    "say it is the closing price and give the date. Never call a stale quote the "
    "current price. "
    "Never ask the user to confirm a purchase in text - the chat box is disabled while an approval "
    "is pending, so a typed 'yes' can never arrive. Call purchase_stock directly; it has its own "
    "approval step, and one call per symbol.",
    financial_tools
)

Memory_Agent = make_agent(
    "Memory_Agent",
    "You are the Memory & General Agent. You handle greetings, general chat, remembering/recalling facts about the user, "
    "and checking the current date and time. Answer general questions directly and nicely. "
    "Only call memory tools when explicitly needed.",
    memory_tools
)

Coding_Agent = make_agent(
    "Coding_Agent",
    "You are the Coding Agent. You delegate repository coding and debugging work to "
    "Cursor Cloud Agents via cursor_coding_agent. "
    "Call cursor_coding_agent with a precise task_description and a github_repo "
    "(org/repo or full URL) when the user names one; otherwise omit github_repo so "
    "the CURSOR_DEMO_REPO default is used. "
    "Summarize the tool result for the user: status, agent id, any PR/branch links, "
    "and the key outcome. Do not invent diffs or claim a PR was opened unless the "
    "tool output says so. Runs can take one to two minutes - tell the user you "
    "started the cloud agent before summarizing the result.",
    coding_tools,
)

Google_Agent = make_agent(
    "Google_Agent",
    "You are the Google Workspace Agent. You manage Gmail and Google Calendar. "
    "You search emails (gmail_search_emails), read specific emails (gmail_read_email), "
    "create draft emails (gmail_create_draft), send emails (gmail_send_email), "
    "list upcoming calendar events (calendar_list_events), create calendar events (calendar_create_event), "
    "and delete calendar events (calendar_delete_event). "
    "Be precise with dates, times, attendees, subjects, and email addresses. "
    "Never ask the user to confirm sending an email or deleting an event in plain text chat - "
    "the chat input is locked while approval is pending, so call gmail_send_email or calendar_delete_event "
    "directly; they have their own interactive approval step.",
    google_tools,
)

# ---------------------------------------------------------------- planning

AGENT_NAMES = (
    "Data_Agent", "Research_Agent", "Financial_Agent", "Memory_Agent", "Coding_Agent", "Google_Agent",
)

# Which agent actually owns each tool. Built from the same lists the agents bind,
# so it cannot drift the way a hand-written mapping would.
TOOL_OWNER = {}
for _owner, _owned in (
    ("Data_Agent", data_tools),
    ("Research_Agent", research_tools),
    ("Financial_Agent", financial_tools),
    ("Memory_Agent", memory_tools),
    ("Coding_Agent", coding_tools),
    ("Google_Agent", google_tools),
):
    for _tool in _owned:
        TOOL_OWNER.setdefault(_tool.name, _owner)


def _reassign_by_named_tool(steps):
    """Send a step to the agent that owns the tool its task names.

    The supervisor plans well and routes badly: it wrote "extract_text_from_image
    for both image paths and return the results" and handed it to Data_Agent,
    which binds only run_pandas_code, inspect_table and plot_data. The agent
    replied "I don't have a tool available to extract text from image files" and
    the step failed with the images still sitting on disk.

    Prompt wording only makes that less likely. When the plan names a tool
    outright it has already said which agent it needs, so honour that instead of
    the agent field it guessed - a naming is evidence, where the routing is not.
    """
    for step in steps:
        task = step["task"].lower()
        named = False
        for tool_name, owner in TOOL_OWNER.items():
            if tool_name in task:
                named = True
                if step["agent"] != owner:
                    step["agent"] = owner
                break
        if not named:
            _reassign_by_task_words(step)
    return steps


# The planner names a tool outright only when it feels like it, and the words it
# writes instead carry the same information. "Find the current temperature of the
# capital" contains no "get_weather", so the step went to Research_Agent, which
# web-searched a number off a Facebook post while the OpenWeather tool sat
# unused. "Multiply it with the temperature" contains no "run_pandas_code", so
# no step ever reached Data_Agent and the product and the share count were both
# done in tokens - the division came out 195,600 when the answer is 195,603.
#
# Only consulted when the step named no tool at all, so an explicit naming still
# wins: a naming is evidence, a keyword is a guess.
# Order matters, and the arithmetic verbs come first: a step reads "multiply it
# with the temperature of that capital", which names both a calculation and a
# fetch. The calculation is what the step DOES - the temperature is a value an
# earlier step already produced - so the verb has to outrank the noun. Matched
# the other way round, that step went to Financial_Agent and got done in prose.
_TASK_WORD_OWNERS = (
    ("Data_Agent", re.compile(
        r"\b(multiply|multiplied|divide|divided|calculat|comput|arithmetic|product of"
        r"|sum of|average of|convert|how many\b.{0,40}\b(buy|afford|purchase))",
        re.IGNORECASE)),
    ("Coding_Agent", re.compile(
        r"\b(cursor_coding_agent|debug|refactor|fix the (bug|code)|open a (pr|pull request)"
        r"|pull request|codebase|github\.com/)\b",
        re.IGNORECASE)),
    ("Financial_Agent", re.compile(
        r"\b(weather|temperature|forecast|humidity|climate|stock price|share price|quote)\b",
        re.IGNORECASE)),
    ("Google_Agent", re.compile(
        r"\b(gmail|emails?|inbox|send email|draft email|calendar|schedule|meeting|events?|appointment)\b",
        re.IGNORECASE)),
)


def _reassign_by_task_words(step):
    for owner, pattern in _TASK_WORD_OWNERS:
        if pattern.search(step["task"]):
            step["agent"] = owner
            return step
    return step

# 4 was too tight once arithmetic became its own step. "Capital, then its area
# and population, then the temperature, multiply, then the stock price, then how
# many shares" is six real steps; capped at four the planner merged them and the
# agents absorbed the missing work as prose, which is where the wrong numbers
# came from.
MAX_PLAN_STEPS    = 6   # a plan longer than this is the model rambling, not planning
MAX_REPLANS       = 1   # one recovery attempt per turn, then answer with what we have
MAX_STEP_ATTEMPTS = 2   # re-dispatch a silent step once, then give up on it
# Must stay above MAX_PLAN_STEPS or the breaker trips partway through a legal
# plan and the turn is synthesized from half its findings.
MAX_DISPATCHES    = MAX_PLAN_STEPS + 3  # hard circuit breaker, counted before any LLM call

# Failure is read out of the agent's own prose, which used to be bare substring
# matching and was wrong in both directions: "error" fired on "no errors in the
# data" and "margin of error", marking successful steps failed and spending the
# turn's only replan; while "could not" missed "couldn't" - the exact wording
# qwen used when it wrongly announced the OCR tool had failed.
#
# First-person phrasing is what separates the two. "I couldn't read the file"
# reports this step failing; "margin of error is 3%" reports a result, and a
# sentence merely mentioning an error is not the agent giving up. The remaining
# non-first-person entries are strings this project's own tools return.
#
# Models write the curly apostrophe, not the ASCII one - the reply in LangSmith
# was "couldn’t" - so both forms have to match.
_APOS = r"['‘’ʼ]?"

_ERROR_PATTERNS = [
    # First-person "I gave up" phrasings.
    r"\bi\s+could\s*n" + _APOS + r"t\b",
    r"\bi\s+could\s+not\b",
    # "can't" is the irregular one: it drops the second n, unlike couldn't,
    # didn't and don't, so it cannot share their shape.
    r"\bi\s+can" + _APOS + r"t\b",
    r"\bi\s+can\s?not\b",
    r"\bi\s+did\s*n" + _APOS + r"t\s+find\b",
    r"\bi\s+do\s*n" + _APOS + r"t\s+have\b",
    r"\bi\s+do\s+not\s+have\b",
    r"\bi\s+was\s+(?:not\s+able|unable)\b",
    r"\bi\s?" + _APOS + r"?m?\s*(?:am\s+)?unable\s+to\b",
    # Strings this project's own tools return on failure.
    r"\berror:",
    r"\bfailed\s+to\b",
    r"\bno\s+results\b",
    r"\bis\s+not\s+set\b",
    r"\bno\s+relevant\s+information\b",
    r"\bno\s+text\s+found\b",
]

_ERROR_PATTERN = re.compile("|".join(_ERROR_PATTERNS), re.IGNORECASE)


class PlanStep(BaseModel):
    agent: Literal[
        "Data_Agent", "Research_Agent", "Financial_Agent", "Memory_Agent", "Coding_Agent", "Google_Agent",
    ] = Field(
        description="Which specialist performs this step."
    )
    task: str = Field(
        description="One imperative sentence describing only this step's job."
    )


class Plan(BaseModel):
    """An ordered plan. Most requests need exactly one step."""
    steps: list[PlanStep]


PLANNER_PROMPT = SystemMessage(content=(
    "You are the Supervisor. Break the user's LATEST request into the SHORTEST ordered "
    "plan of specialist steps that fully answers it.\n"
    "- Data_Agent: spreadsheets, CSVs, charts, math.\n"
    # Images belong here explicitly. Without them named, the planner had no
    # reason to send an attached screenshot to Research_Agent, so rule 1 caught
    # it instead and a two-image turn was planned as "ask the user to upload the
    # images" - to Memory_Agent, which has no OCR tool.
    "- Research_Agent: web search, fetching URLs, Wikipedia, uploaded RAG PDFs, "
    "reading text out of attached images and screenshots.\n"
    "- Financial_Agent: stock quotes and history, stock purchases, weather.\n"
    "- Memory_Agent: greetings, general chat, remembering/recalling user facts, date and time.\n"
    "- Coding_Agent: repository coding, debugging, refactors, and opening PRs via "
    "Cursor Cloud Agents (cursor_coding_agent).\n"
    "- Google_Agent: Gmail (search, read, draft, send emails) and Google Calendar (list, schedule, delete events).\n\n"
    "Rules:\n"
    "1. Greetings, chit-chat and anything answerable directly = ONE step to Memory_Agent.\n"
    "2. Only add a second step when it genuinely needs a DIFFERENT specialist "
    "(e.g. fetch stock history, then chart it).\n"
    "3. Never split one specialist's own tool sequence into multiple steps - an agent "
    "can call several of its own tools inside one step.\n"
    f"4. At most {MAX_PLAN_STEPS} steps. Fewer is better.\n"
    # Routing is reassigned from the tool name when the step names one, so
    # naming it is the cheapest way to get the step to the right agent.
    "5. Name the exact tool the step needs (get_weather, run_pandas_code, "
    "get_stock_price, web_search, wikipedia, extract_text_from_image, "
    "cursor_coding_agent, gmail_search_emails, gmail_read_email, gmail_create_draft, "
    "gmail_send_email, calendar_list_events, calendar_create_event, calendar_delete_event). "
    "Routing follows the tool you name.\n"
    # No agent has a calculator in its head. Every number an LLM 'computes' in
    # prose is a guess that happens to look like a digit, and one of them came
    # out 195,600 when the answer was 195,603.
    "6. ANY arithmetic - multiply, divide, compare two quantities, convert units - "
    "is its own run_pandas_code step for Data_Agent. Never leave a calculation "
    "inside a step that is really about looking something up.\n"
    "7. Codebase fix/debug/refactor/PR requests = ONE step to Coding_Agent with "
    "cursor_coding_agent. Do not send those to Research_Agent.\n"
    "8. Gmail or Calendar requests (checking emails, sending emails, creating drafts, "
    "listing/creating/deleting calendar events) = ONE step to Google_Agent."
))


def _turn_key(messages):
    """Stable id for the current user turn, so we plan once per turn."""
    for m in reversed(messages):
        if isinstance(m, HumanMessage):
            return getattr(m, "id", None) or "h%d" % hash(str(m.content))
    return ""


def _last_human_text(messages):
    for m in reversed(messages):
        if isinstance(m, HumanMessage):
            return str(m.content)
    return ""


def _last_agent_text(messages):
    """The plain-text reply that sent control back here, if any."""
    m = messages[-1]
    if isinstance(m, AIMessage) and not getattr(m, "tool_calls", None) and str(m.content).strip():
        return str(m.content).strip()
    return None


def _dispatches_this_turn(messages):
    count = 0
    for m in reversed(messages):
        if isinstance(m, HumanMessage):
            break
        if isinstance(m, AIMessage) and not getattr(m, "tool_calls", None):
            count += 1
    return count


def _tool_succeeded_this_step(messages):
    """Did a tool return without erroring since this step was dispatched?

    Walks back from the agent's reply and stops at the boundary of this step -
    the previous step's plain reply, or the user's turn. Traffic from an earlier
    step belongs to that step and says nothing about this one.

    ToolNode only sets status="error" when a tool raises, and the tools here
    report most problems as ordinary strings instead. So a True here means "a
    tool ran and did not blow up", which is enough to veto a failure guessed
    from prose, but never enough to declare a step successful on its own.
    """
    for message in reversed(messages[:-1]):
        if isinstance(message, HumanMessage):
            return False
        if isinstance(message, AIMessage) and not getattr(message, "tool_calls", None) \
                and str(message.content).strip():
            return False
        if isinstance(message, ToolMessage) and getattr(message, "status", "success") != "error":
            return True
    return False


EVIDENCE_PER_STEP  = 3    # tool results carried forward from one step
EVIDENCE_CHARS     = 300  # per result, before the synthesizer's own budget


def _tool_results_this_step(messages):
    """The raw tool output produced since this step was dispatched.

    A step's note is the agent's own prose, and prose drops things. One
    Financial_Agent step called get_weather and then get_stock_price, wrote a
    reply about the stock and never mentioned the weather - so the synthesizer,
    which sees notes and nothing else, told the user the temperature "was not
    provided in the findings" while 34.35C sat in a ToolMessage a few turns back.

    Carrying the numbers themselves also gives the synthesizer something it can
    copy, which is the only way its "never calculate" rule can actually be kept.

    Boundary logic matches _tool_succeeded_this_step: walk back to the previous
    step's plain reply or the user's turn, because traffic before that belongs
    to a different step.
    """
    found = []
    for message in reversed(messages[:-1]):
        if isinstance(message, HumanMessage):
            break
        if isinstance(message, AIMessage) and not getattr(message, "tool_calls", None) \
                and str(message.content).strip():
            break
        if isinstance(message, ToolMessage):
            found.append("%s -> %s" % (getattr(message, "name", "tool"),
                                       _brief(message.content, EVIDENCE_CHARS)))
            if len(found) >= EVIDENCE_PER_STEP:
                break
    return list(reversed(found))


def _looks_failed(text):
    return bool(_ERROR_PATTERN.search(text[:300]))


def _looks_like_question(text):
    t = text.strip()
    return t.endswith("?") and len(t) < 400


# The note a step leaves is the ONLY thing the synthesizer ever sees - the tool
# output behind it is not passed on. At 280 characters a step reporting three
# figures lost the third, and the synthesizer filled the gap from its own head.
def _brief(text, n=600):
    t = " ".join(str(text).split())
    return t[:n] + ("..." if len(t) > n else "")


def _legacy_route(messages):
    """Old single-word router. Only used if structured planning fails outright."""
    try:
        response = llm.invoke(
            [SystemMessage(content=(
                "Reply with exactly one of: Data_Agent, Research_Agent, "
                "Financial_Agent, Memory_Agent, Coding_Agent, Google_Agent."
            )), HumanMessage(content=_last_human_text(messages))],
            config={"callbacks": []},
        )
        content = response.content.strip().upper()
        for agent in AGENT_NAMES:
            if agent.upper() in content:
                return agent
    except Exception:
        pass
    return "Memory_Agent"


def _is_provider_failure(error):
    """Is this the provider refusing, rather than the planner having no idea?

    Checked structurally as well as by base class so a second provider does not
    quietly rejoin the swallow path: every SDK here raises errors carrying an
    HTTP status, while a schema or parse failure carries none.
    """
    return isinstance(error, GroqError) or hasattr(error, "status_code")


def _make_plan(messages):
    try:
        plan = llm.with_structured_output(Plan).invoke(
            [PLANNER_PROMPT, HumanMessage(content=_last_human_text(messages))],
            config={"callbacks": []},
        )
        steps = _reassign_by_named_tool([
            {"agent": s.agent, "task": s.task.strip(), "status": "pending", "note": ""}
            for s in plan.steps if s.agent in AGENT_NAMES and s.task.strip()
        ][:MAX_PLAN_STEPS])
    except Exception as error:
        # A provider failure is not a planning outcome. Swallowed here, a 429
        # took the same path as "the planner returned nothing usable":
        # _legacy_route picked an agent by keyword and the step's task became
        # the raw user text - file note and all - with nothing surfaced to
        # anyone. Every turn near the daily token cap then looked like the agent
        # being stupid rather than the provider declining to answer. The turn
        # handler in the UI already renders a raised error, and Groq's own retry
        # budget is spent by the time one reaches here.
        if _is_provider_failure(error):
            raise
        steps = []
    if not steps:
        steps = [{"agent": _legacy_route(messages), "task": _last_human_text(messages),
                  "status": "pending", "note": ""}]
    return steps


def _replan(messages, plan, cursor):
    """One recovery plan for the steps that have not run yet."""
    failed = plan[cursor]
    try:
        revised = llm.with_structured_output(Plan).invoke([
            PLANNER_PROMPT,
            HumanMessage(content=(
                f"Original request: {_last_human_text(messages)}\n"
                f"Step {cursor + 1} ({failed['agent']}: {failed['task']}) FAILED with:\n"
                f"{failed.get('note', '')}\n\n"
                "Plan the remaining steps using a DIFFERENT approach. If nothing can "
                "recover this, return zero steps."
            )),
        ], config={"callbacks": []})
        return [{"agent": s.agent, "task": s.task.strip(), "status": "pending", "note": ""}
                for s in revised.steps if s.agent in AGENT_NAMES][:MAX_PLAN_STEPS]
    except Exception:
        return []


def _task_brief(plan, cursor):
    """What the dispatched agent is told - the step, plus what earlier steps found."""
    step = plan[cursor]
    lines = [
        "Step %d of %d: %s" % (cursor + 1, len(plan), step["task"]),
        "You are %s. Do ONLY this step, then stop." % step["agent"],
    ]
    # Data_Agent exists so that arithmetic goes through a calculator instead of
    # through token prediction, and it quietly skips the tool whenever the sum
    # looks easy - the same plan ran 1,100,000 x 34.35 through run_pandas_code
    # on one turn and in prose on the next. Say it in the step itself, where it
    # cannot be trimmed out of history.
    if step["agent"] == "Data_Agent":
        lines.append(
            "Compute this with run_pandas_code and assign the answer to `result`. "
            "Do NOT work the arithmetic out in prose - a number you write without "
            "calling the tool is a guess, however simple the sum looks."
        )
    done = [s for s in plan[:cursor] if s.get("note")]
    if done:
        lines.append("Already established by earlier steps:")
        # The tool output rides along with the prose here too - a step told to
        # "multiply it by the temperature" needs the digits, and the previous
        # agent's summary is where digits go missing.
        for s in done[-2:]:
            lines.append("  - [%s] %s" % (s["agent"], s["note"]))
            lines += ["      [tool] %s" % ev for ev in (s.get("evidence") or [])]
    if cursor + 1 < len(plan):
        lines.append("A later step (%s) will handle: %s"
                     % (plan[cursor + 1]["agent"], plan[cursor + 1]["task"]))
    return "\n".join(lines)


def _out(next_, plan, cursor, turn, replans, attempts, task=""):
    return {"next": next_, "plan": plan, "plan_cursor": cursor, "plan_for": turn,
            "replans": replans, "step_attempts": attempts, "current_task": task}


def _dispatch(plan, cursor, turn, replans, attempts):
    plan[cursor]["status"] = "running"
    return _out(plan[cursor]["agent"], plan, cursor, turn, replans, attempts,
                _task_brief(plan, cursor))


def _finish(plan, cursor, turn, replans):
    return _out("synthesizer", plan, cursor, turn, replans, 0)


def supervisor_node(state: Chatstate):
    messages = state.get("messages") or []
    turn     = _turn_key(messages)
    plan     = state.get("plan") or []
    cursor   = state.get("plan_cursor", 0)
    replans  = state.get("replans", 0)
    attempts = state.get("step_attempts", 0)

    # Circuit breaker first, so a runaway turn costs no tokens.
    if _dispatches_this_turn(messages) >= MAX_DISPATCHES:
        return _finish(plan, cursor, turn, replans)

    # New user turn -> plan once, dispatch step 1.
    if state.get("plan_for") != turn or not plan:
        plan = _make_plan(messages)
        return _dispatch(plan, 0, turn, 0, 0)

    if cursor >= len(plan):
        return _finish(plan, cursor, turn, replans)

    reply = _last_agent_text(messages)
    step  = plan[cursor]

    # Agent produced nothing usable - retry the step once, then abandon it.
    if reply is None:
        attempts += 1
        if attempts >= MAX_STEP_ATTEMPTS:
            step["status"] = "failed"
            cursor += 1
            return (_finish(plan, cursor, turn, replans) if cursor >= len(plan)
                    else _dispatch(plan, cursor, turn, replans, 0))
        return _dispatch(plan, cursor, turn, replans, attempts)

    # The agent needs the human. Stop the turn - the chat box is the only way forward.
    if _looks_like_question(reply):
        step["status"] = "blocked"
        step["note"] = _brief(reply)
        return _finish(plan, cursor, turn, replans)

    # The step failed. One recovery plan, then live with the result.
    #
    # _looks_failed reads the agent's prose, which is the least reliable thing
    # in the turn: qwen was handed a clean 297-character OCR result and wrote
    # "I couldn't extract any text from the image", which marked a successful
    # step failed and spent the turn's only replan on it. A tool that ran and
    # did not error outranks whatever the model said about it.
    if _looks_failed(reply) and not _tool_succeeded_this_step(messages) \
            and replans < MAX_REPLANS:
        step["status"] = "failed"
        step["note"] = _brief(reply)
        tail = _replan(messages, plan, cursor)
        if tail:
            plan = plan[:cursor + 1] + tail
            return _dispatch(plan, cursor + 1, turn, replans + 1, 0)
        return _finish(plan, cursor + 1, turn, replans + 1)

    # A maths step that called no tool is not done, it is unattempted. Prose
    # arithmetic is the single failure this whole pipeline was built to stop, so
    # accepting it here would let the number the user actually asked for be a
    # guess dressed as a result. Re-dispatch once; _task_brief names the tool.
    #
    # Deliberately not routed through _looks_failed and the replan budget: the
    # step is fine and the plan is fine, the agent just skipped its tool, and
    # replanning would rewrite a plan that was already correct.
    if step["agent"] == "Data_Agent" and not _tool_succeeded_this_step(messages) \
            and attempts + 1 < MAX_STEP_ATTEMPTS:
        return _dispatch(plan, cursor, turn, replans, attempts + 1)

    # Normal completion: record a note the next step can read, advance. The note
    # is prose and prose loses numbers, so the tool output behind it rides along.
    step["status"] = "done"
    step["note"] = _brief(reply)
    step["evidence"] = _tool_results_this_step(messages)
    cursor += 1
    if cursor >= len(plan):
        return _finish(plan, cursor, turn, replans)
    return _dispatch(plan, cursor, turn, replans, 0)

def synthesizer_node(state: Chatstate):
    """A final pass that composes one coherent answer from all completed steps."""
    plan = state.get("plan") or []
    if len(plan) <= 1:
        return {"next": "FINISH"}

    # On a multi-step turn the UI mutes the specialists and lets only this node
    # speak, so bailing out here would leave the user staring at a finished plan
    # and no answer at all. Blocked and failed steps carry the notes that
    # explain why - a question the agent needs answered, or the error it hit -
    # and those are exactly what the user has to see when nothing completed.
    # Each finding is the agent's summary followed by the tool output it came
    # from. The raw results are what the synthesizer is allowed to copy digits
    # out of; the prose is only there for context.
    completed_notes = []
    for s in plan:
        if s.get('status') != 'done' or not s.get('note'):
            continue
        completed_notes.append(f"- {s['agent']}: {s['note']}")
        completed_notes += [f"    [tool] {ev}" for ev in (s.get('evidence') or [])]
    unfinished_notes = [f"- {s['agent']} ({s.get('status')}): {s.get('note', '')}"
                        for s in plan if s.get('status') in ('blocked', 'failed') and s.get('note')]
    if not completed_notes and not unfinished_notes:
        return {"messages": [AIMessage(content=(
            "I planned this out but none of the steps produced a usable result. "
            "Could you rephrase the request, or narrow it to one part?"
        ))], "sender": "Synthesizer", "next": "FINISH"}

    prompt = SystemMessage(content=(
        "You are the final Synthesizer. Based on the steps executed by the agents, "
        "write a single, cohesive answer to the user's original request. "
        "Do not list the steps you took, just answer the question naturally. "
        "If a chart or table was already presented, just refer to it, do not redraw it.\n"
        # The synthesizer sees step notes, never the tool calls behind them, so
        # any arithmetic it performs is done in tokens with no calculator in
        # reach. Handed "42,000,000" and "214.72" it wrote the share count three
        # times with three different decimal tails, all of them wrong: it said
        # 195,600 where the answer is 195,603. A model that may only copy
        # numbers cannot get them wrong.
        "NEVER calculate. Every number in your answer must appear verbatim in the "
        "findings below - copy the digits, do not re-derive them, and do not round, "
        "rescale or convert them. This holds even when the arithmetic looks trivial: "
        "asked for 33,000,000 divided by 202.55 the last run wrote 162,923, a figure "
        "no step ever produced. If answering needs arithmetic that no step performed, "
        "say plainly which figure is missing instead of working it out yourself. "
        # Refusing is correct; inventing a reason for refusing is not. Sitting on
        # a product of 37,785,000 it declined the share count because "the
        # investment budget was not provided" - the budget was the number
        # directly above it in the same findings.
        "When you do refuse, re-read the findings first and name the step that "
        "should have produced the figure. Never invent a reason, and never claim "
        "something is missing that is already listed below.\n"
        # Two sources for one quantity is normal; passing both to the user is
        # not. A run that had a live quote AND a month-old page answered with a
        # branch per price and left the user to pick, which is the one job the
        # final node exists to do.
        "GIVE ONE ANSWER. If the findings offer two values for the same quantity, a "
        "figure fetched by a dedicated tool (get_weather, get_stock_price, "
        "run_pandas_code) always beats one quoted from a web page, and a dated "
        "figure beats an undated one. Pick the winner, answer with it, and note the "
        "discarded value in a single trailing sentence. Never present parallel "
        "scenarios and never ask the user to choose.\n"
        # Each agent already streamed its own reply this turn. Restating the
        # whole workflow underneath them is what made one answer appear three
        # times over.
        "Answer once. Do not restate the plan, and do not summarise your own "
        "answer again at the end."
    ))

    parts = [f"Original request: {_last_human_text(state.get('messages', []))}"]
    if completed_notes:
        parts.append("Agent findings:\n" + "\n".join(completed_notes))
    if unfinished_notes:
        parts.append(
            "Steps that did NOT complete - tell the user what is missing, and if one "
            "of these is a question, put that question to them:\n"
            + "\n".join(unfinished_notes)
        )
    human_req = HumanMessage(content="\n\n".join(parts))

    response = llm.invoke([prompt, human_req])
    return {"messages": [response], "sender": "Synthesizer", "next": "FINISH"}

# Tool Node
tool_node = ToolNode(tools)

if cloud_store.ENABLED:
    connection = None
    checkpoint = cloud_store.checkpoint()
else:
    connection = sqlite3.connect(database="agentic_chatbot.db", check_same_thread=False)
    checkpoint = SqliteSaver(connection)
_ensure_memory_table()

graph = StateGraph(Chatstate)

# Nodes
graph.add_node("supervisor", supervisor_node)
graph.add_node("Data_Agent", Data_Agent)
graph.add_node("Research_Agent", Research_Agent)
graph.add_node("Financial_Agent", Financial_Agent)
graph.add_node("Memory_Agent", Memory_Agent)
graph.add_node("Coding_Agent", Coding_Agent)
graph.add_node("Google_Agent", Google_Agent)
graph.add_node("tools", tool_node)
graph.add_node("synthesizer", synthesizer_node)

# Routing Logic
def route_from_supervisor(state: Chatstate):
    # Now it dictates exact step or routes to synthesizer on _finish
    return state.get("next", "Memory_Agent")

def route_from_agent(state: Chatstate):
    # Text replies go back to the supervisor so it can hand off to another
    # specialist; the loop guards live in supervisor_node.
    last_message = state["messages"][-1]
    if hasattr(last_message, "tool_calls") and last_message.tool_calls:
        return "tools"
    return "supervisor"

def route_after_tool(state: Chatstate):
    return state.get("sender", "Memory_Agent")

# Edges
graph.add_edge(START, "supervisor")

graph.add_conditional_edges("supervisor", route_from_supervisor, {
    "Data_Agent": "Data_Agent",
    "Research_Agent": "Research_Agent",
    "Financial_Agent": "Financial_Agent",
    "Memory_Agent": "Memory_Agent",
    "Coding_Agent": "Coding_Agent",
    "Google_Agent": "Google_Agent",
    "synthesizer": "synthesizer"
})

graph.add_edge("synthesizer", END)

for agent in [
    "Data_Agent", "Research_Agent", "Financial_Agent", "Memory_Agent", "Coding_Agent", "Google_Agent",
]:
    graph.add_conditional_edges(agent, route_from_agent, {
        "tools": "tools",
        "supervisor": "supervisor"
    })

graph.add_conditional_edges("tools", route_after_tool, {
    "Data_Agent": "Data_Agent",
    "Research_Agent": "Research_Agent",
    "Financial_Agent": "Financial_Agent",
    "Memory_Agent": "Memory_Agent",
    "Coding_Agent": "Coding_Agent",
    "Google_Agent": "Google_Agent",
})

chatbot =graph.compile(checkpointer=checkpoint)

if __name__ == "__main__":
    print("🤖 Agentic Chatbot CLI\n")
    print("Type 'exit' to quit.\n")

    # thread_id still works with MemorySaver (conversation kept in RAM)
    thread_id = "demo-thread"

    while True:
        user_input = input("You: ")
        if user_input.lower().strip() in {"exit", "quit"}:
            print("Goodbye!")
            break

        config = {"configurable": {"thread_id": thread_id}}
        state_snapshot = chatbot.get_state(config)
        
        # Check if graph is currently interrupted
        if state_snapshot.next:
            # Resume it with the user's input
            result = chatbot.invoke(Command(resume=user_input), config=config)
        else:
            # Start a new turn
            state = {"messages": [HumanMessage(content=user_input)]}
            result = chatbot.invoke(state, config=config)

        # Check if graph paused again after invocation
        new_state = chatbot.get_state(config)
        if new_state.next and new_state.tasks and new_state.tasks[0].interrupts:
            # It was interrupted. Print the interrupt message.
            interrupt_val = new_state.tasks[0].interrupts[0].value
            print(f"System: {interrupt_val}\n")
        else:
            # Completed the run. Print the assistant's final response
            latest_message = result["messages"][-1]
            print(f"Assistant: {latest_message.content}\n")
