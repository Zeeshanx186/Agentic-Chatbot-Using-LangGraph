# pyrefly: ignore [missing-import]
import backend as backend
# pyrefly: ignore [missing-import]
from backend import chatbot, ingest_rag_document, tools
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.types import Command

# pyrefly: ignore [missing-import]
import chat_store

chat_store.bind(backend)

import sys
if sys.platform == 'win32':
    # Streamlit starts Uvicorn/Tornado before this script runs, so setting the 
    # WindowsSelectorEventLoopPolicy here is too late. Instead, we monkey-patch
    # the Proactor pipe transport to ignore ConnectionResetError on disconnects.
    import asyncio
    from asyncio.proactor_events import _ProactorBasePipeTransport
    
    _original_ccl = _ProactorBasePipeTransport._call_connection_lost
    def _silenced_ccl(self, exc):
        try:
            _original_ccl(self, exc)
        except ConnectionResetError:
            pass
    _ProactorBasePipeTransport._call_connection_lost = _silenced_ccl

import datetime
import json
import os
import re
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
    /* ==========================================================================
       Nuclear Streamlit Sidebar Styles
       ========================================================================== */
    [data-testid="stSidebar"] {
        background-color: #111114 !important;
        border-right: 1px solid rgba(255, 255, 255, 0.08) !important;
    }

    /* 1. Main Viewport Flex Column: Enforce 100vh full height */
    [data-testid="stSidebarContent"],
    [data-testid="stSidebarUserContent"],
    [data-testid="stSidebar"] > div:first-child {
        display: flex !important;
        flex-direction: column !important;
        height: 100vh !important;
        max-height: 100vh !important;
        padding: 0.75rem 0.6rem !important;
        box-sizing: border-box !important;
        overflow: hidden !important;
    }

    /* 2. Intermediate Streamlit block wrappers must stretch 100% height */
    [data-testid="stSidebarContent"] > [data-testid="stVerticalBlock"],
    [data-testid="stSidebarUserContent"] > [data-testid="stVerticalBlock"],
    [data-testid="stSidebarContent"] > div > [data-testid="stVerticalBlock"],
    [data-testid="stSidebarUserContent"] > div > [data-testid="stVerticalBlock"],
    [data-testid="stSidebarContent"] [data-testid="stVerticalBlockBorderWrapper"],
    [data-testid="stSidebarUserContent"] [data-testid="stVerticalBlockBorderWrapper"] {
        display: flex !important;
        flex-direction: column !important;
        flex: 1 1 100% !important;
        height: 100% !important;
        min-height: 0 !important;
        gap: 0 !important;
    }

    /* 3. Nuke the Sidebar Gaps (The Chat History Fix) */
    [data-testid="stSidebarUserContent"] [data-testid="stVerticalBlock"],
    [data-testid="stSidebarContent"] [data-testid="stVerticalBlock"] {
        gap: 0rem !important;
    }
    [data-testid="stSidebarUserContent"] [data-testid="element-container"],
    [data-testid="stSidebarUserContent"] [data-testid="stElementContainer"],
    [data-testid="stSidebarContent"] [data-testid="element-container"],
    [data-testid="stSidebarContent"] [data-testid="stElementContainer"] {
        margin: 0px !important;
        padding: 0px !important;
        gap: 0px !important;
    }

    /* 4. Flatten and Compact the Chat Buttons */
    [data-testid="stSidebarUserContent"] .stButton > button,
    [data-testid="stSidebarContent"] .stButton > button {
        border: none !important;
        background-color: transparent !important;
        box-shadow: none !important;
        padding: 6px 12px !important;
        height: auto !important;
        min-height: 32px !important;
        text-align: left !important;
        justify-content: flex-start !important;
        width: 100% !important;
        border-radius: 6px !important;
        color: #b4b4b4 !important;
        font-size: 0.85rem !important;
        font-weight: 400 !important;
        transition: background-color 0.12s ease, color 0.12s ease !important;
    }
    [data-testid="stSidebarUserContent"] .stButton > button:hover,
    [data-testid="stSidebarContent"] .stButton > button:hover {
        background-color: #2b2b33 !important; /* Subtle hover state */
        color: #ffffff !important;
    }

    /* Active Chat Highlight */
    [class*="st-key-chatrow_active_"] .stButton > button {
        background-color: rgba(255, 255, 255, 0.12) !important;
        color: #ffffff !important;
        font-weight: 500 !important;
    }

    /* 5. Text Truncation for Long Chat Titles */
    [data-testid="stSidebarUserContent"] .stButton > button p,
    [data-testid="stSidebarContent"] .stButton > button p {
        overflow: hidden !important;
        white-space: nowrap !important;
        text-overflow: ellipsis !important;
        margin: 0 !important;
        width: 100% !important;
        text-align: left !important;
    }
    [data-testid="stSidebarUserContent"] .stButton > button div,
    [data-testid="stSidebarContent"] .stButton > button div,
    [data-testid="stSidebarUserContent"] .stButton > button [data-testid="stMarkdownContainer"],
    [data-testid="stSidebarContent"] .stButton > button [data-testid="stMarkdownContainer"] {
        width: 100% !important;
        min-width: 0 !important;
        justify-content: flex-start !important;
        text-align: left !important;
    }

    /* Compact chat row wrapper */
    [class*="st-key-chatrow_"],
    [class*="st-key-chatrow_active_"] {
        border-radius: 6px !important;
        padding-right: 2px !important;
        margin-bottom: 2px !important;
        min-height: 32px !important;
        height: 32px !important;
        align-items: center !important;
        transition: background-color 0.12s ease !important;
    }
    [class*="st-key-chatrow_"]:hover {
        background: rgba(255, 255, 255, 0.04) !important;
    }
    [class*="st-key-chatrow_active_"] {
        background: rgba(255, 255, 255, 0.08) !important;
    }

    /* Popover menu icon in row */
    [class*="st-key-chatrow_"] .stPopover button,
    [class*="st-key-chatrow_active_"] .stPopover button {
        background: transparent !important;
        border: none !important;
        box-shadow: none !important;
        min-height: 28px !important;
        height: 28px !important;
        padding: 0 4px !important;
        color: #71717a !important;
    }
    [class*="st-key-chatrow_"] .stPopover button:hover,
    [class*="st-key-chatrow_active_"] .stPopover button:hover {
        color: #ffffff !important;
    }
    [class*="st-key-chatrow_"] .stPopover {
        opacity: 0 !important;
        transition: opacity 0.15s ease !important;
    }
    [class*="st-key-chatrow_"]:hover .stPopover,
    [class*="st-key-chatrow_active_"] .stPopover {
        opacity: 1 !important;
    }

    /* Top Action: New chat button override */
    [class*="st-key-newchat"] {
        flex-shrink: 0 !important;
        margin-bottom: 4px !important;
    }
    [class*="st-key-newchat"] .stButton > button {
        display: flex !important;
        align-items: center !important;
        justify-content: flex-start !important;
        border-radius: 8px !important;
        min-height: 38px !important;
        height: 38px !important;
        background: rgba(255, 255, 255, 0.07) !important;
        border: 1px solid rgba(255, 255, 255, 0.1) !important;
        color: #f0f0f0 !important;
        font-weight: 500 !important;
        font-size: 0.88em !important;
        padding: 0 12px !important;
        gap: 10px !important;
        width: 100% !important;
        box-shadow: 0 1px 2px rgba(0, 0, 0, 0.15) !important;
        transition: all 0.15s cubic-bezier(0.4, 0, 0.2, 1) !important;
    }
    [class*="st-key-newchat"] .stButton > button:hover {
        background: rgba(255, 255, 255, 0.13) !important;
        border-color: rgba(255, 255, 255, 0.2) !important;
        color: #ffffff !important;
        transform: translateY(-1px);
        box-shadow: 0 3px 8px rgba(0, 0, 0, 0.25) !important;
    }
    [class*="st-key-newchat"] .stButton > button:active {
        transform: translateY(0);
    }
    [class*="st-key-newchat"] .stButton > button [data-testid="stIconMaterial"] {
        font-size: 18px !important;
        color: #d1d5db !important;
    }

    /* Search chats input */
    [data-testid="stSidebar"] [data-testid="stTextInput"] {
        margin-top: 0.2rem !important;
        margin-bottom: 0.2rem !important;
        flex-shrink: 0 !important;
    }
    [data-testid="stSidebar"] [data-testid="stTextInput"] > div > div {
        background-color: rgba(255, 255, 255, 0.04) !important;
        border: 1px solid rgba(255, 255, 255, 0.08) !important;
        border-radius: 8px !important;
        transition: all 0.15s ease !important;
        min-height: 36px !important;
        height: 36px !important;
    }
    [data-testid="stSidebar"] [data-testid="stTextInput"] input {
        color: #e5e7eb !important;
        font-size: 0.85rem !important;
        padding-left: 0.5rem !important;
    }
    [data-testid="stSidebar"] [data-testid="stTextInput"] input::placeholder {
        color: #71717a !important;
        font-size: 0.85rem !important;
    }
    [data-testid="stSidebar"] [data-testid="stTextInput"] > div > div:focus-within {
        border-color: rgba(140, 123, 255, 0.7) !important;
        background-color: rgba(255, 255, 255, 0.07) !important;
        box-shadow: 0 0 0 2px rgba(140, 123, 255, 0.15) !important;
    }
    [data-testid="stSidebar"] [data-testid="stTextInput"] [data-testid="stIconMaterial"] {
        color: #71717a !important;
        font-size: 18px !important;
    }

    /* Middle Independently Scrollable Section */
    [class*="st-key-sidebar_recents_scroll"] {
        flex: 1 1 auto !important;
        height: 100% !important;
        min-height: 0 !important;
        overflow-y: auto !important;
        overflow-x: hidden !important;
        margin: 0.25rem 0 !important;
        padding-right: 4px !important;
        display: flex !important;
        flex-direction: column !important;
        gap: 0 !important;
    }

    /* Slim custom scrollbar */
    [class*="st-key-sidebar_recents_scroll"]::-webkit-scrollbar {
        width: 5px !important;
    }
    [class*="st-key-sidebar_recents_scroll"]::-webkit-scrollbar-track {
        background: transparent !important;
    }
    [class*="st-key-sidebar_recents_scroll"]::-webkit-scrollbar-thumb {
        background: rgba(255, 255, 255, 0.15) !important;
        border-radius: 9999px !important;
    }
    [class*="st-key-sidebar_recents_scroll"]::-webkit-scrollbar-thumb:hover {
        background: rgba(255, 255, 255, 0.28) !important;
    }

    /* Bottom Anchored Footer ("Agent tools") */
    [class*="st-key-sidebar_footer"] {
        margin-top: auto !important;
        flex-shrink: 0 !important;
        width: 100% !important;
        padding-top: 0.5rem !important;
        border-top: 1px solid rgba(255, 255, 255, 0.08) !important;
    }
    [class*="st-key-sidebar_footer"] [data-testid="stPopoverButton"] button,
    [class*="st-key-sidebar_footer"] .stButton > button {
        display: flex !important;
        align-items: center !important;
        justify-content: flex-start !important;
        width: 100% !important;
        height: 38px !important;
        min-height: 38px !important;
        border-radius: 8px !important;
        background: rgba(255, 255, 255, 0.04) !important;
        border: 1px solid rgba(255, 255, 255, 0.08) !important;
        color: #d1d5db !important;
        font-size: 0.86rem !important;
        font-weight: 500 !important;
        padding: 0 12px !important;
        gap: 10px !important;
        transition: all 0.15s ease !important;
    }
    [class*="st-key-sidebar_footer"] [data-testid="stPopoverButton"] button:hover,
    [class*="st-key-sidebar_footer"] .stButton > button:hover {
        background: rgba(255, 255, 255, 0.09) !important;
        border-color: rgba(255, 255, 255, 0.16) !important;
        color: #ffffff !important;
    }
    [class*="st-key-sidebar_footer"] [data-testid="stIconMaterial"] {
        font-size: 18px !important;
        color: #9ca3af !important;
    }
    [class*="st-key-sidebar_footer"] [data-testid="stPopoverButton"] div[aria-hidden="true"] {
        display: none !important;
    }

    /* Responsive mobile drawer */
    @media (max-width: 768px) {
        [data-testid="stSidebarContent"],
        [data-testid="stSidebarUserContent"],
        [data-testid="stSidebar"] > div:first-child {
            height: calc(100vh - 3.5rem) !important;
            padding: 0.75rem 0.5rem !important;
        }
    }

    /* ---------- Model picker: chip in the composer, list in the dropdown ----------
       Streamlit exposes no theme CSS variables at runtime, so every colour here
       is a color-mix on currentColor: it resolves correctly in both themes,
       where the old var(--background-color) rules silently resolved to nothing. */

    /* The chip, pinned inside the chat input bar. Fixed positioning inherits the
       main column's centred x, so only the left inset needs setting. */
    [class*="st-key-modelpicker_container"] {
        position: fixed;
        /* Centres the 30px chip in the 54px tall bar. No negative margin
           here on purpose: the popover anchors to the layout box, so a
           painted-but-shifted chip made the dropdown overlap it. */
        bottom: 64px;
        margin-left: 46px;     /* clears the attachment button */
        z-index: 9999;
        width: fit-content;
    }
    [class*="st-key-modelpicker_container"] [data-testid="stPopoverButton"] {
        min-height: 30px !important;
        height: 30px;
        padding: 0 10px !important;
        border-radius: 999px;
        max-width: 140px;      /* paired with the textarea inset below */
        gap: 6px;
        justify-content: flex-start;
        font-weight: 400;
        background: color-mix(in srgb, currentColor 7%, transparent);
        border: 1px solid color-mix(in srgb, currentColor 16%, transparent);
        box-shadow: none;
        transition: background-color 0.15s ease, border-color 0.15s ease;
    }
    [class*="st-key-modelpicker_container"] [data-testid="stPopoverButton"]:hover {
        background: color-mix(in srgb, currentColor 13%, transparent);
        border-color: color-mix(in srgb, currentColor 28%, transparent);
    }
    [class*="st-key-modelpicker_container"] [data-testid="stPopoverButton"] > div {
        justify-content: flex-start;
        gap: 6px;
        width: 100%;
    }
    [class*="st-key-modelpicker_container"] [data-testid="stPopoverButton"] p {
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
        font-size: 0.82em;
        margin: 0;
    }
    /* Provider glyph and chevron are chip furniture, not the label. */
    [class*="st-key-modelpicker_container"] [data-testid="stPopoverButton"] [data-testid="stIconMaterial"] {
        font-size: 17px;
    }
    [class*="st-key-modelpicker_container"] [data-testid="stPopoverButton"] div[aria-hidden="true"] {
        opacity: 0.55;
    }

    /* The dropdown panel. The popover body is portaled out to the document
       body, so the container key never reaches it - :has() on a row key is what
       scopes these rules to the model list and leaves the chat-row and copy
       popovers alone. Never write a tag-like token in a comment here: a literal
       angle-bracket body tag made DOMPurify drop this entire style block. */
    [data-testid="stPopoverBody"]:has([class*="st-key-pickmodel_"]) {
        width: 264px;
        min-width: 264px;
        /* Baseweb sets max-height inline, so this needs the override. */
        max-height: 300px !important;
        padding: 6px;
        border-radius: 10px;
        border: 1px solid color-mix(in srgb, currentColor 16%, transparent);
        box-shadow: 0 10px 28px rgba(0, 0, 0, 0.18);
        overflow-y: auto;
    }
    [data-testid="stPopoverBody"]:has([class*="st-key-pickmodel_"]) [data-testid="stVerticalBlock"] {
        gap: 1px;
    }
    [data-testid="stPopoverBody"]:has([class*="st-key-pickmodel_"])::-webkit-scrollbar {
        width: 8px;
    }
    [data-testid="stPopoverBody"]:has([class*="st-key-pickmodel_"])::-webkit-scrollbar-thumb {
        background: color-mix(in srgb, currentColor 22%, transparent);
        border-radius: 4px;
    }
    /* Provider heading: a dim line between groups, not a row of its own. */
    [data-testid="stPopoverBody"]:has([class*="st-key-pickmodel_"]) [data-testid="stCaptionContainer"] p {
        font-size: 0.7em;
        opacity: 0.5;
        margin: 6px 0 2px 8px;
    }
    /* One row per model: 30px tall, icon left, ellipsised label. */
    [class*="st-key-pickmodel_"] button {
        background: transparent;
        border: none;
        box-shadow: none;
        justify-content: flex-start;
        min-height: 30px;
        height: 30px;
        padding: 0 8px;
        border-radius: 6px;
        font-weight: 400;
        gap: 8px;
        transition: background-color 0.12s ease;
    }
    [class*="st-key-pickmodel_"] button:hover {
        background: color-mix(in srgb, currentColor 9%, transparent);
    }
    /* The selected row keeps its provider icon and takes a fill instead: the
       old check swap moved the glyph and made one row read differently. */
    [class*="st-key-pickmodel_active_"] button {
        background: color-mix(in srgb, currentColor 14%, transparent);
        font-weight: 500;
    }
    [class*="st-key-pickmodel_"] button > div {
        justify-content: flex-start;
        gap: 8px;
        width: 100%;
    }
    [class*="st-key-pickmodel_"] button [data-testid="stIconMaterial"] {
        font-size: 17px;
    }
    [class*="st-key-pickmodel_"] button p {
        overflow: hidden;
        white-space: nowrap;
        text-overflow: ellipsis;
        text-align: left;
        font-size: 0.86em;
        margin: 0;
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
    /* Keep typed text clear of the chip: 46px inset + 140px chip cap + 12px. */
    [data-testid="stChatInput"] textarea {
        padding-left: 152px !important;
    }

    /* ---------- Visual polish ---------- */

    html {
        scroll-behavior: smooth;
    }

    /* Inter Bold at Streamlit's default 44px h1 size renders chunky and
       fringed under Chromium's anti-aliasing - too heavy for a product
       header. Smaller, lighter, tighter tracking reads calmer. Sizes/weights
       are set via headingFontSizes/headingFontWeights in config.toml; this
       covers what config.toml can't (smoothing, tracking). */
    h1, h2, h3, h4, h5, h6 {
        -webkit-font-smoothing: antialiased;
        -moz-osx-font-smoothing: grayscale;
        text-rendering: optimizeLegibility;
        letter-spacing: -0.01em;
    }

    /* Chat bubbles: config.toml can't shape spacing/shadow, only color. */
    [data-testid="stChatMessage"] {
        border-radius: 14px;
        padding: 4px 6px;
        animation: msg-in 0.22s ease-out;
    }
    @keyframes msg-in {
        from { opacity: 0; transform: translateY(6px); }
        to   { opacity: 1; transform: none; }
    }
    @media (prefers-reduced-motion: reduce) {
        [data-testid="stChatMessage"] { animation: none; }
    }

    /* Empty-state hero: vertically and horizontally center "How can I help?"
       and suggestion pills in the viewport. */
    [class*="st-key-emptystate"] {
        max-width: 680px;
        margin: 18vh auto 0 auto;
        padding-bottom: 24px;
        text-align: center;
        display: flex;
        flex-direction: column;
        align-items: center;
        justify-content: center;
    }
    [class*="st-key-emptystate"] [data-testid="stElementContainer"] {
        display: flex;
        justify-content: center;
        width: 100%;
    }
    [class*="st-key-emptystate"] [data-testid="stHeadingContainer"],
    [class*="st-key-emptystate"] [data-testid="stCaptionContainer"],
    [class*="st-key-emptystate"] [data-testid="stMarkdownContainer"] {
        text-align: center !important;
        display: flex;
        justify-content: center;
        align-items: center;
        width: 100%;
    }
    [class*="st-key-emptystate"] [data-testid="stButtonGroup"] {
        display: flex;
        justify-content: center;
        flex-wrap: wrap;
        width: 100%;
    }

    /* Composer: soft elevation and a hairline top border instead of the flat
       default, so it reads as a distinct bar rather than part of the scroll. */
    [data-testid="stChatInput"] {
        border-radius: 16px;
        box-shadow: 0 -2px 16px color-mix(in srgb, currentColor 6%, transparent);
        transition: box-shadow 0.15s ease, border-color 0.15s ease;
    }
    [data-testid="stChatInput"]:focus-within {
        box-shadow: 0 -2px 20px color-mix(in srgb, currentColor 10%, transparent);
    }

    /* Suggestion pills on the empty state get a lift on hover instead of the
       default flat swap. st.pills is a themed stButtonGroup under the hood. */
    [data-testid="stButtonGroup"] button {
        transition: transform 0.12s ease, background-color 0.12s ease;
    }
    [data-testid="stButtonGroup"] button:hover {
        transform: translateY(-1px);
    }

    /* App-wide themed scrollbar (the model popover already had its own). */
    ::-webkit-scrollbar {
        width: 8px;
        height: 8px;
    }
    ::-webkit-scrollbar-thumb {
        background: color-mix(in srgb, currentColor 18%, transparent);
        border-radius: 4px;
    }
    ::-webkit-scrollbar-thumb:hover {
        background: color-mix(in srgb, currentColor 28%, transparent);
    }

    /* Keyboard focus should be visible; Streamlit's default outline is easy to
       lose against custom backgrounds. Streamlit exposes no theme CSS
       variable to reach for here (see the note above), so this uses the same
       currentColor trick as the rest of the file. */
    button:focus-visible,
    [data-testid="stChatInput"] textarea:focus-visible,
    input:focus-visible {
        outline: 2px solid color-mix(in srgb, currentColor 55%, transparent);
        outline-offset: 2px;
    }

    /* Running tool/status icon gets a subtle pulse on top of its own spin -
       st.status is a themed stExpander under the hood, and
       stExpanderIconSpinner only renders for the running state (it's swapped
       for stExpanderIconCheck on completion), so this only fires while work
       is actually in progress. */
    [data-testid="stExpanderIconSpinner"] {
        animation: status-pulse 1.6s ease-in-out infinite;
    }
    @keyframes status-pulse {
        0%, 100% { opacity: 1; }
        50%      { opacity: 0.5; }
    }
    @media (prefers-reduced-motion: reduce) {
        [data-testid="stExpanderIconSpinner"] {
            animation: none;
        }
    }

    /* Narrow viewports: the model chip's fixed desktop offsets (46px left
       inset, 140px width cap) overlap the composer placeholder text and can
       clip against the attachment button once the column shrinks below the
       chat's own tablet breakpoint. */
    @media (max-width: 640px) {
        [class*="st-key-modelpicker_container"] {
            margin-left: 40px;
        }
        [class*="st-key-modelpicker_container"] [data-testid="stPopoverButton"] {
            max-width: 96px;
        }
        [data-testid="stChatInput"] textarea {
            padding-left: 108px !important;
        }
    }
</style>
""")

CHATS_BEFORE_FOLD = 10

# One row per tool: (icon, running label, done label). This was two parallel
# dicts keyed identically, which KeyErrors the moment one of them drifts.
# Order matters - tool_meta matches on substring, so stock_history has to be
# checked before stock_price, and both before purchase.
TOOLS_UI = {
    "search":        (":material/travel_explore:", "Searching the web", "Searched the web"),
    "fetch_url":     (":material/link:", "Reading the page", "Read the page"),
    "extract_text":  (":material/document_scanner:", "Reading the image", "Read the image"),
    "datetime":      (":material/schedule:", "Checking the date", "Checked the date"),
    "weather":       (":material/partly_cloudy_day:", "Checking the weather", "Checked the weather"),
    "stock_history": (":material/ssid_chart:", "Pulling price history", "Pulled price history"),
    "stock_price":   (":material/trending_up:", "Looking up the stock price", "Looked up the stock price"),
    "purchase":      (":material/shopping_cart:", "Placing the order", "Placed the order"),
    "retriever":     (":material/description:", "Searching your documents", "Searched your documents"),
    "wikipedia":     (":material/menu_book:", "Reading Wikipedia", "Read Wikipedia"),
    "inspect":       (":material/table_view:", "Inspecting the file", "Inspected the file"),
    "pandas":        (":material/function:", "Extracting spreadsheet data", "Extracted spreadsheet data"),
    "plot":          (":material/bar_chart:", "Drawing the chart", "Drew the chart"),
    "remember":      (":material/bookmark_add:", "Saving to memory", "Saved to memory"),
    "recall":        (":material/psychology:", "Checking memory", "Checked memory"),
    "forget":        (":material/delete_sweep:", "Clearing memory", "Cleared memory"),
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


# Streamlit's markdown reads `$...$` as LaTeX, so a reply containing two prices
# turns everything between them into mangled math: "$450,000 mortgage at 6.375%
# ... $2,807.41" renders as "450,000mortgageat6.375...2,807.41". A dollar sign
# followed by a digit is currency in this app, never math - `$x^2$` and `$$...$$`
# both stay untouched, the latter because the second `$` is preceded by one.
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


# Small enough to read as an attachment rather than as content, large enough to
# recognise the screenshot. Applied to the container, never to st.image.
THUMBNAIL_WIDTH = 170


def render_thumbnails(paths):
    """Draw attachment images as one right-aligned row of thumbnails.

    The size lives on the surrounding container on purpose. A pixel width passed
    to st.image makes Streamlit re-encode the served file at that size, which
    leaves the built-in Fullscreen button with nothing to enlarge; sizing the
    container instead keeps the thumbnail small while the full-resolution image
    stays one click away.

    Paths, not bytes: histories live in session state and re-render on every
    interaction, so holding decoded images would cost the file size per
    attachment for the life of the session.
    """
    with st.container(horizontal=True, horizontal_alignment="right", gap="small"):
        for path in paths:
            with st.container(width=THUMBNAIL_WIDTH):
                st.image(path, width="stretch")


def render_attachments(records):
    """Replay an upload turn as one collapsed row.

    Collapsed by default because the interesting thing about an attachment is
    that it arrived, not where on disk it landed. The absolute path is still one
    click away - the agent works from it, so it is what you need when a turn
    misbehaves.
    """
    records = records or []
    failed = [r for r in records if r.get("kind") == "error"]
    plural = "" if len(records) == 1 else "s"

    if failed:
        label = "%d of %d file%s attached" % (len(records) - len(failed), len(records), plural)
        state = "error"
    else:
        label = "Attached %d file%s" % (len(records), plural)
        state = "complete"

    with st.status(label, state=state, type="compact", expanded=False):
        for record in records:
            kind = record.get("kind")
            if kind == "error":
                st.markdown(":material/error: **%s** - %s"
                            % (record["name"], record.get("detail", "could not be read")))
            elif kind == "indexed":
                st.markdown(":material/menu_book: **%s** - added to the knowledge base"
                            % record["name"])
            else:
                st.markdown("**%s**" % record["name"])
                st.code(record.get("path", ""), language=None, wrap_lines=True)


def tool_meta(tool_name):
    """Map a raw tool name to (running label, done label, Material icon)."""
    name = (tool_name or "tool").lower()
    for key, (icon, running, done) in TOOLS_UI.items():
        if key in name:
            return running, done, icon
    return f"Running {tool_name}", f"Used {tool_name}", ":material/build:"


# Both of these ran on every rerun, for every extract still in the transcript:
# a disk read plus a full CSV re-encode just to redraw a table that has not
# changed. Keyed by the extract id, which is immutable once written.
@st.cache_data(show_spinner=False, max_entries=50)
def extract_frame(extract_id):
    return backend.load_extract(extract_id)


@st.cache_data(show_spinner=False, max_entries=50)
def extract_csv(extract_id):
    df = extract_frame(extract_id)
    return b"" if df is None else df.to_csv(index=False).encode("utf-8")


CHART_DRAWERS = ("line_chart", "bar_chart", "area_chart", "scatter_chart")


def render_tool_result(container, content, key_suffix=""):
    # plot_data returns a receipt too - the spec is on disk and the frame it
    # points at is already cached, so the chart is drawn from those, not from
    # anything the model wrote.
    chart_id = backend.chart_id_in(content)
    if chart_id:
        spec = backend.load_chart(chart_id)
        df = extract_frame(spec["extract_id"]) if spec else None
        # The spec is a JSON file on disk, so "kind" is checked against the
        # allowed drawers rather than handed straight to getattr.
        draw_name = (spec or {}).get("kind", "") + "_chart"
        if spec is not None and df is not None and draw_name in CHART_DRAWERS:
            draw = getattr(container, draw_name)
            draw(df, x=spec["x"], y=spec["y"])
            return
        container.caption("This chart is no longer on disk.")
        return

    # Bulk extractions never travel through the model: the tool wrote the table
    # to disk and returned only a receipt, so render the real thing here.
    extract_id = backend.extract_id_in(content)
    if extract_id:
        df = extract_frame(extract_id)
        if df is not None:
            container.caption("%d rows x %d columns" % (df.shape[0], df.shape[1]))
            container.dataframe(df, height=360)
            container.download_button(
                "Download CSV",
                extract_csv(extract_id),
                file_name="extract_%s.csv" % extract_id,
                mime="text/csv",
                icon=":material/download:",
                key="dl_%s_%s" % (extract_id, key_suffix),
            )
            return
        container.caption("This extraction is no longer on disk.")

    # st.json's frontend viewer needs an object or an array. Handed a bare
    # scalar it renders "src property must be a valid json object" and nothing
    # else - and a bare scalar is exactly what run_pandas_code returns for
    # `result = 1100000 * 34.35`, so three successful calculations in a row all
    # displayed as errors while the model was reading the right numbers.
    # json.loads accepts scalars happily, so the type has to be checked after.
    try:
        parsed = json.loads(content)
    except Exception:
        parsed = None

    if isinstance(parsed, (dict, list)):
        container.json(parsed)
    else:
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


UPLOAD_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "uploads")


def save_upload(uploaded_file):
    """Persist an uploaded spreadsheet and return its absolute path.

    The Excel tools take a file path and the agent may come back to the file on
    a later turn, so these cannot live in a temp file that gets deleted.
    """
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", uploaded_file.name)
    path = os.path.join(UPLOAD_DIR, safe_name)

    # Never clobber a file the agent may still be referencing.
    stem, extension = os.path.splitext(path)
    counter = 1
    while os.path.exists(path):
        path = "%s_%d%s" % (stem, counter, extension)
        counter += 1

    with open(path, "wb") as f:
        f.write(uploaded_file.getvalue())
    return path


def new_thread_id():
    return str(uuid.uuid4())


def start_new_chat(rerun=True):
    """Open an empty conversation. Called from the sidebar fragment, so the
    rerun has to be app-scoped or the main area keeps the old transcript."""
    thread_id = new_thread_id()
    st.session_state.thread_id = thread_id
    st.session_state.setdefault("histories", {})[thread_id] = []
    threads = st.session_state.setdefault("threads", [])
    if thread_id not in threads:
        threads.insert(0, thread_id)
    if rerun:
        st.rerun(scope="app")


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

    if "feedback" not in st.session_state:
        st.session_state.feedback = chat_store.load_feedback()

    if "threads" not in st.session_state:
        # One pass over the checkpoint log yields ids, times and previews
        # together - see chat_store.thread_summaries for why it is not one
        # get_state per thread.
        summaries = chat_store.thread_summaries()
        st.session_state.threads = [s["id"] for s in summaries]
        st.session_state.summaries = {s["id"]: s for s in summaries}

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
        # Skipping image messages here keeps a chat that opened with an
        # attachment from being titled with a file path.
        if msg["role"] == "user" and msg.get("type") != "image":
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


def remember_feedback(widget_key, thread_id, index):
    """Persist a thumbs rating so it is still there on the next visit."""
    value = st.session_state.get(widget_key)
    st.session_state.feedback.setdefault(thread_id, {})
    if value is None:
        st.session_state.feedback[thread_id].pop(str(index), None)
    else:
        st.session_state.feedback[thread_id][str(index)] = value
    chat_store.save_feedback(thread_id, index, value)


def queue_prompt(text):
    st.session_state.pending_prompt = text
    st.rerun()


init_state()

# ============================== Sidebar ==============================

# The sidebar is the heaviest part of a rerun: one button, a popover, a text
# input and two more buttons per conversation. As a fragment, searching,
# renaming and expanding the list redraw the sidebar alone instead of
# re-rendering the whole transcript. Actions that change what the main area
# shows rerun with scope="app".
@st.fragment
def date_bucket(when):
    """Which sidebar heading a thread belongs under.

    Buckets rather than dates: with 87 threads the useful question is "was this
    today or last week", and a date per row would repeat itself down the list.
    Threads whose checkpoint predates the ts field land in Older rather than
    being dropped.
    """
    if when is None:
        return "Older"

    days = (datetime.date.today() - when.date()).days
    if days <= 0:
        return "Today"
    if days == 1:
        return "Yesterday"
    if days < 7:
        return "Previous 7 days"
    if days < 30:
        return "Previous 30 days"
    return "Older"


BUCKET_ORDER = ("Today", "Yesterday", "Previous 7 days", "Previous 30 days", "Older")


def row_label(thread_id):
    """Single-line title matching ChatGPT sidebar style."""
    return chat_title(thread_id)


# One Material Symbol per provider. Deliberately not the real OpenAI/Anthropic/
# Google marks: brand assets carry usage rules, and an approximated logo is worse
# than an honest icon. Everything else in this app already uses Material Symbols.
PROVIDER_ICONS = {
    "groq": ":material/bolt:",
    "google": ":material/auto_awesome:",
    "openrouter": ":material/hub:",
    "openai": ":material/blur_on:",
    "ollama": ":material/terminal:",
}
PROVIDER_FALLBACK_ICON = ":material/smart_toy:"


def provider_icon(provider):
    return PROVIDER_ICONS.get(provider, PROVIDER_FALLBACK_ICON)


def render_model_picker():
    """Current model as a trigger; the catalog as an icon list inside a popover.

    st.selectbox cannot put an icon on an option, and with 21 models across three
    providers a flat list of "label · note" strings gave every row the same
    shape. A popover of buttons can carry a per-row icon and provider headings,
    which is what makes the list scannable.
    """
    models = MODELS
    by_id = {m["id"]: m for m in models}

    if st.session_state.model_id not in by_id:
        # Catalog changed under us (cache expiry, key removed); fall back.
        st.session_state.model_id = models[0]["id"]

    active = by_id[st.session_state.model_id]

    with st.popover(active["label"], icon=provider_icon(active["provider"]),
                    width="content", key="modelpicker"):
        current_note = None
        for model in models:
            # Models arrive grouped by provider from available_models(), so a
            # heading is needed only where the note changes.
            if model["note"] != current_note:
                st.caption(model["note"])
                current_note = model["note"]

            is_active = model["id"] == st.session_state.model_id
            if st.button(
                model["label"],
                # The active row carries its own key prefix so CSS can fill it.
                # A check icon in place of the provider glyph was the old
                # marker; it made the selected row the one row that read
                # differently from every other.
                key=("pickmodel_active_" if is_active else "pickmodel_") + model["id"],
                icon=provider_icon(model["provider"]),
                type="tertiary",
                width="stretch",
                help=model["id"],
            ):
                st.session_state.model_id = model["id"]
                apply_model()
                st.rerun(scope="app")


def render_sidebar():
    with st.sidebar:
        # New chat button
        if st.button("New chat", key="newchat", type="secondary",
                     icon=":material/edit_square:", width="stretch"):
            start_new_chat()

        search_query = st.text_input(
            "Search chats",
            placeholder="Search chats",
            icon=":material/search:",
            label_visibility="collapsed",
        )

        matching = [t for t in st.session_state.threads if matches_search(t, search_query)]

        show_all = bool(search_query) or st.session_state.get("show_all_chats", False)
        visible_threads = matching if show_all else matching[:CHATS_BEFORE_FOLD]

        # Middle scrollable container for conversation history
        with st.container(key="sidebar_recents_scroll"):
            st.markdown("<div style='color: #888; font-size: 11px; font-weight: 600; letter-spacing: 1px; margin-top: 15px; margin-bottom: 5px; padding-left: 12px;'>RECENTS</div>", unsafe_allow_html=True)

            if not matching:
                st.caption("No chats match that search." if search_query else "No chats yet.")

            for thread_id in visible_threads:
                is_active = thread_id == st.session_state.thread_id
                row_key = ("chatrow_active_" if is_active else "chatrow_") + thread_id

                with st.container(key=row_key, horizontal=True, gap=None,
                                  vertical_alignment="center"):
                    if st.button(row_label(thread_id), key="open_" + thread_id, width="stretch"):
                        st.session_state.thread_id = thread_id
                        st.rerun(scope="app")

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
                            st.toast("Chat renamed", icon=":material/check:")
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
                            st.toast("Chat deleted", icon=":material/delete:")
                            st.rerun(scope="app")

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

        # Bottom anchored footer for Agent Tools
        with st.container(key="sidebar_footer"):
            with st.popover("Agent tools", icon=":material/build:", width="stretch", key="nav_tools"):
                st.markdown("**Active agent capabilities**")
                for tool in tools:
                    tool_name = getattr(tool, "name", str(tool))
                    _, _, icon = tool_meta(tool_name)
                    st.badge(tool_name, icon=icon, color="gray")


render_sidebar()


# ============================== Main area ==============================

messages = history()
thread_key = st.session_state.thread_id
suggested_prompt = None

with st.container(horizontal=True, vertical_alignment="center"):
    st.title(":material/smart_toy: Agentic chatbot", width="stretch")
    st.badge(st.session_state.model_id, icon=":material/bolt:", color="primary")

if not messages:
    with st.container(key="emptystate", horizontal_alignment="center"):
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
        items = group["items"]
        item_index = 0
        while item_index < len(items):
            item = items[item_index]
            kind = item.get("type")

            if kind == "image":
                # Consecutive image messages are one attachment set. Rendering
                # them one at a time would stack a thumbnail per line; collecting
                # the run puts them on a single row.
                run = []
                while item_index < len(items) and items[item_index].get("type") == "image":
                    run.append(items[item_index]["content"])
                    item_index += 1
                render_thumbnails(run)
                continue

            if kind == "tool":
                _, done_label, icon = tool_meta(item.get("tool_name"))
                has_chart = backend.chart_id_in(item["content"]) is not None
                with st.status(icon + " " + done_label, state="complete",
                               type="compact", expanded=has_chart) as box:
                    render_tool_result(box, item["content"],
                                       key_suffix="%d_%d" % (group_index, item_index))
            elif kind == "files":
                render_attachments(item["content"])
            else:
                st.markdown(markdown_safe(item["content"]))
                last_text = item["content"]

            item_index += 1

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

                feedback_key = "feedback_%s_%d" % (thread_key, group_index)
                # st.feedback has no value argument, so a saved rating has to be
                # seeded into session state before the widget is created.
                if feedback_key not in st.session_state:
                    saved = st.session_state.feedback.get(thread_key, {})
                    st.session_state[feedback_key] = saved.get(str(group_index))
                st.feedback(
                    "thumbs",
                    key=feedback_key,
                    on_change=remember_feedback,
                    args=(feedback_key, thread_key, group_index),
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

with st.container(key="modelpicker_container"):
    render_model_picker()

user_input = st.session_state.pop("pending_prompt", None) or suggested_prompt
uploaded_files = []

# Only the Approve/Decline buttons may answer an interrupt. Any other resume
# value is compared against "yes" in the backend and silently declines the
# purchase, so a queued prompt waits for the next turn instead of becoming
# the answer.
if is_interrupted and user_input:
    st.session_state.pending_prompt = user_input
    user_input = None

# len(messages) is not usable for this below: the upload loop appends to
# messages before the turn block runs, so a chat that opens with a file
# upload would never get a title.
had_user_message = any(m["role"] == "user" for m in messages)

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
        file_type=["pdf", "xlsx", "xlsm", "xls", "csv", "png", "jpg", "jpeg"],
        submit_mode="stop",
    )

    if submitted:
        if isinstance(submitted, str):
            user_input = submitted
        else:
            user_input = getattr(submitted, "text", "") or user_input
            uploaded_files = getattr(submitted, "files", []) or []

attached_paths = []
attached_images = []
# PDFs never reach attached_paths - they are chunked into FAISS and the temp file
# is deleted - so they need their own list to be mentioned to the agent at all.
indexed_names = []

# Kept separate from the chat_input file_type list because the two answer
# different questions: that list is what the widget accepts, this is what can be
# drawn back to the user in their own bubble.
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg"}

# One record per uploaded file, persisted as a single message. Previously each
# file produced its own assistant bubble spelling out the absolute save path,
# which cost four lines of Windows path per attachment directly above the image
# it described. The path is for the agent; the user needs the filename.
attachments = []

if uploaded_files:
    plural = "" if len(uploaded_files) == 1 else "s"
    with st.chat_message("assistant", avatar=":material/smart_toy:"):
        with st.status("Attaching %d file%s" % (len(uploaded_files), plural),
                       expanded=True) as status:
            for uploaded_file in uploaded_files:
                extension = os.path.splitext(uploaded_file.name)[1].lower()

                if extension == ".pdf":
                    # PDFs go into the FAISS index; retriever_tool answers from there.
                    st.write("Indexing " + uploaded_file.name)
                    tmp_path = None
                    try:
                        with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp_file:
                            tmp_file.write(uploaded_file.getvalue())
                            tmp_path = tmp_file.name
                        # The upload's own name, not the temp file it arrived in:
                        # it is the only thing a retrieved chunk can be
                        # attributed to once the temp file is deleted below.
                        ingest_rag_document(tmp_path, uploaded_file.name)
                        indexed_names.append(uploaded_file.name)
                        attachments.append({"name": uploaded_file.name, "path": "",
                                            "kind": "indexed", "detail": ""})
                    except Exception as e:
                        attachments.append({"name": uploaded_file.name, "path": "",
                                            "kind": "error", "detail": str(e)})
                    finally:
                        if tmp_path and os.path.exists(tmp_path):
                            os.remove(tmp_path)
                else:
                    # Spreadsheets and images stay on disk: inspect_table,
                    # run_pandas_code and extract_text_from_image take a path, and
                    # the agent may revisit the file on a later turn.
                    st.write("Saving " + uploaded_file.name)
                    try:
                        saved_path = save_upload(uploaded_file)
                        attached_paths.append(saved_path)
                        if extension in IMAGE_EXTENSIONS:
                            attached_images.append(saved_path)
                        attachments.append({"name": uploaded_file.name, "path": saved_path,
                                            "kind": "saved", "detail": ""})
                    except Exception as e:
                        attachments.append({"name": uploaded_file.name, "path": "",
                                            "kind": "error", "detail": str(e)})

            failed = [a for a in attachments if a["kind"] == "error"]
            if failed:
                status.update(
                    label="%d of %d file%s attached" % (
                        len(attachments) - len(failed), len(attachments), plural),
                    state="error", expanded=False)
            else:
                status.update(label="Attached %d file%s" % (len(attachments), plural),
                              state="complete", expanded=False)

    messages.append({"role": "assistant", "type": "files", "content": attachments})

# What the user reads back in their own bubble, which is not what the agent
# reads. The agent needs absolute paths to call inspect_table / run_pandas_code /
# extract_text_from_image; the user needs their own sentence, with the
# attachment shown above it rather than spelled out as a Windows path.
display_input = user_input

# Notes are joined into ONE paragraph below rather than stacked. strip_file_note
# only removes the first note it finds, so two of them left the second one
# showing in the user's own bubble.
turn_notes = []

# An indexed PDF has no path, so it cannot ride along in the ALREADY SAVED note
# and used to be mentioned nowhere at all: the agent was handed a bare question
# about a document it had no reason to believe existed, and answered "I don't
# see any file attached" with retriever_tool sitting bound to the same call.
if indexed_names:
    turn_notes.append(chat_store.indexed_note(indexed_names))

if attached_paths:
    # One path per line rather than a comma-joined run. Two paths on one line
    # read to the planner as the user *mentioning* images, and it planned "ask
    # the user to upload the images" - to an agent with no OCR tool - while the
    # files sat on disk. Saying they are already saved, and that asking again is
    # wrong, is what stops that; the listing is what makes several of them
    # enumerable instead of one long string.
    listing = "\n".join("- " + path for path in attached_paths)

    # Describe the work, never the tools. An earlier version of this note named
    # both extract_text_from_image and inspect_table/run_pandas_code, and the
    # supervisor routed an images-only turn to Data_Agent purely because that
    # agent owns the two spreadsheet tools the sentence mentioned - the plan
    # then read "extract_text_from_image for both image paths" against an agent
    # with no such tool. Saying only what the files are lets the supervisor
    # match them against its own agent descriptions.
    sheets = [path for path in attached_paths if path not in attached_images]
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
        user_input = note + '\n\n' + user_input
    else:
        user_input = note + '\n\nSummarise what each file contains.'
        # An attachment with no question still needs a user bubble, otherwise the
        # turn opens with the assistant replying to nothing visible.
        display_input = "Attached " + ", ".join(
            [os.path.basename(path) for path in attached_paths] + indexed_names
        )

# ============================== Turn execution ==============================

PLAN_ICONS = {
    "pending": ":material/radio_button_unchecked:",
    "running": ":material/pending:",
    "done": ":material/check_circle:",
    "failed": ":material/error:",
    "blocked": ":material/help:",
}

# Nodes whose tokens are meant for the user. The supervisor plans with
# callbacks=[] so its structured output should never reach the stream, but
# plan JSON leaking into the answer bubble is bad enough to guard explicitly.
SPEAKING_NODES = {"Data_Agent", "Research_Agent", "Financial_Agent",
                  "Memory_Agent", "synthesizer"}


def render_plan(slot, plan):
    """Redraw the whole plan into an st.empty() slot, replacing the last draw."""
    # A one-step plan is the planner's fallback; showing it is noise.
    if not plan or len(plan) <= 1:
        return
    box = slot.container(border=True)
    box.caption(":material/checklist: Plan")
    for i, step in enumerate(plan, 1):
        icon = PLAN_ICONS.get(step.get("status", "pending"), PLAN_ICONS["pending"])
        box.markdown("%s &nbsp;**%d. %s** - %s"
                     % (icon, i, step.get("agent", "?"), step.get("task", "")))


def filter_think(stream, tool_area, plan_slot):
    """Yield visible assistant text, hiding <think> blocks and surfacing tools.

    Tool UI goes into tool_area, a container made before st.write_stream starts.
    Creating elements inline here instead would land them in the middle of the
    container write_stream is streaming into, and the text stops updating live.

    The stream runs in stream_mode=["messages", "updates"], so every item is a
    (mode, payload) pair: "messages" carries (chunk, metadata), "updates"
    carries {node_name: state_delta}. The updates channel is what makes the
    plan visible while the turn is still running.
    """
    in_think = False
    buffer = ""
    status_container = None
    current_node = None
    spoke = False          # has anything been yielded for current_node yet
    multi_step = False     # does this turn end at the synthesizer?

    for mode, payload in stream:

        if mode == "updates":
            for node_name, delta in (payload or {}).items():
                if node_name == "supervisor" and delta and "plan" in delta:
                    render_plan(plan_slot, delta["plan"])
                    # A plan with more than one step ends at the synthesizer,
                    # which rewrites everything the specialists already said.
                    # Letting both speak printed one answer three times over -
                    # once per agent that ran, then again in full. The plan
                    # arrives on this channel before any agent streams a token,
                    # so there is time to mute them.
                    multi_step = len(delta["plan"] or []) > 1
            continue

        message_chunk, metadata = payload
        node = (metadata or {}).get("langgraph_node")

        # A specialist handing off mid-turn gets its own paragraph. Without
        # this, two agents' replies are concatenated into one run-on block.
        if node and node != current_node:
            if buffer and not in_think:
                yield buffer
                spoke = True
            buffer, in_think = "", False
            if current_node is not None and spoke:
                yield "\n\n"
            current_node = node
            spoke = False

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

        elif isinstance(message_chunk, ToolMessage):
            tool_name = getattr(message_chunk, "name", "tool")
            _, done, icon = tool_meta(tool_name)
            done_label = icon + " " + done

            if status_container is not None:
                shows_chart = backend.chart_id_in(message_chunk.content) is not None
                status_container.update(label=done_label, state="complete",
                                        expanded=shows_chart)
                render_tool_result(status_container, message_chunk.content,
                                   key_suffix="live%d" % len(messages))
                status_container = None
            else:
                with tool_area.status(done_label, state="complete", type="compact") as box:
                    render_tool_result(box, message_chunk.content,
                                       key_suffix="live%d" % len(messages))

            messages.append({
                "role": "assistant",
                "type": "tool",
                "tool_name": tool_name,
                "content": message_chunk.content,
            })

        if isinstance(message_chunk, AIMessage) and message_chunk.content:
            # Tool status still surfaces for muted agents above - only their
            # prose is dropped, so the user watches the work without reading
            # the same answer once per specialist.
            speakers = {"synthesizer"} if multi_step else SPEAKING_NODES
            if node is not None and node not in speakers:
                continue

            content = message_chunk.content
            # Some providers stream content blocks instead of a plain string,
            # and buffer += [...] is a TypeError that kills the whole turn.
            if isinstance(content, list):
                content = "".join(p.get("text", "")
                                  for p in content if isinstance(p, dict))
            if not content:
                continue

            buffer += content
            while True:
                if not in_think:
                    start_idx = buffer.find("<think>")
                    if start_idx != -1:
                        if start_idx > 0:
                            yield buffer[:start_idx]
                            spoke = True
                        buffer = buffer[start_idx + len("<think>"):]
                        in_think = True
                    else:
                        # Hold back 6 chars: the longest proper prefix of
                        # "<think>" a chunk boundary could split off.
                        if len(buffer) > 6:
                            yield buffer[:-6]
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

    if not in_think and buffer:
        yield buffer


if user_input:
    thread_id = st.session_state.thread_id
    is_first_message = not had_user_message

    if not is_interrupted:
        # Appended before the text so the replay loop above draws the image on
        # top of the sentence, matching the order they are rendered here.
        for image_path in attached_images:
            messages.append({"role": "user", "type": "image", "content": image_path})

        messages.append({"role": "user", "type": "text", "content": display_input})
        with st.chat_message("user"):
            if attached_images:
                render_thumbnails(attached_images)
            st.markdown(markdown_safe(display_input))
    else:
        # The interrupt disappears from graph state once resumed, so without
        # this the transcript never records that a purchase was approved.
        decision = "Approved." if user_input == "yes" else "Declined."
        messages.append({"role": "user", "type": "text", "content": decision})
        with st.chat_message("user"):
            st.markdown(decision)

    if is_first_message:
        # A placeholder now, not an LLM round-trip: generate_title() here made
        # the user watch an empty screen for a second before streaming began.
        # The real title is fetched after the answer is on screen.
        #
        # display_input, not user_input: the latter carries the injected file
        # note, which titled eight separate threads "Attached file(s): D:\Persona..."
        # and made every image chat indistinguishable in the sidebar.
        st.session_state.titles[thread_id] = (
            chat_store.title_from_attachments(attached_paths)
            or chat_store.fallback_title(display_input)
        )

    with st.chat_message("assistant", avatar=":material/smart_toy:"):
        stream_input = (
            Command(resume=user_input)
            if is_interrupted
            else {"messages": [HumanMessage(content=user_input)]}
        )
        # The plan is drawn above the tools, both above the streaming answer.
        # plan_slot is a live element: it is gone after the next rerun, so the
        # plan is only visible while the turn is running.
        plan_slot = st.empty()
        tool_area = st.container()
        try:
            ai_message = st.write_stream(
                filter_think(
                    chatbot.stream(stream_input, config=CONFIG,
                                   stream_mode=["messages", "updates"]),
                    tool_area,
                    plan_slot,
                )
            )
        except Exception as e:
            # A provider error mid-turn used to surface as a traceback and take
            # the tool results collected so far down with it. The failure goes
            # into the transcript rather than an st.error, because the rerun at
            # the end of this script would discard a bare error element.
            ai_message = None
            messages.append({
                "role": "assistant",
                "type": "text",
                "content": ":material/error: **The turn failed.** " + str(e),
            })

    if ai_message:
        messages.append({"role": "assistant", "type": "text", "content": ai_message})
    elif not any(m.get("type") == "text" and m["role"] == "assistant"
                 for m in messages[-2:]):
        messages.append({
            "role": "assistant",
            "type": "text",
            "content": (
                ":material/info: The model returned no text this turn - it spent "
                "the reply on reasoning. Ask again, or pick another model."
            ),
        })

    if is_first_message:
        # Titled from what the user typed, not from the file note prepended for
        # the agent - the model was reading the note and returning "Image
        # Content Inquiry" for every image thread, five of them identical.
        # An attachment names itself. Skipping the LLM here is not only a
        # better title - it removes a model round-trip from every upload turn.
        title = (chat_store.title_from_attachments(attached_paths)
                 or chat_store.generate_title(display_input))
        st.session_state.titles[thread_id] = title
        chat_store.save_title(thread_id, title)

    if thread_id not in st.session_state.threads:
        st.session_state.threads.insert(0, thread_id)

    st.rerun()
