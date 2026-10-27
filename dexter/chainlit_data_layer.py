"""Custom Chainlit data layer backed by chat_store.py.

Delegates thread listing, deletion, renaming, and feedback to the same
persistence helpers the Chainlit UI uses (agentic_chatbot.db / chat_titles.json
/ chat_feedback.json, or cloud_store when enabled).

Step and element persistence is handled by LangGraph's checkpointer, so the
corresponding methods here are intentional no-ops.
"""

import uuid
from typing import Dict, List, Optional

from chainlit.data.base import BaseDataLayer
from chainlit.element import Element, ElementDict
from chainlit.step import StepDict
from chainlit.types import (
    Feedback,
    PageInfo,
    PaginatedResponse,
    Pagination,
    ThreadDict,
    ThreadFilter,
)
from chainlit.user import PersistedUser, User

from dexter import chat_store

# A single pseudo-user for this app (no auth).
_FIXED_USER_ID = "default"
_FIXED_USER_IDENTIFIER = "user"
_FIXED_USER = PersistedUser(
    id=_FIXED_USER_ID,
    identifier=_FIXED_USER_IDENTIFIER,
    createdAt="2024-01-01T00:00:00Z",
)


class ChatStoreDataLayer(BaseDataLayer):
    """Chainlit data layer that delegates to chat_store.py."""

    # ----------------------------------------------------------------- users

    async def get_user(self, identifier: str) -> Optional[PersistedUser]:
        return _FIXED_USER

    async def create_user(self, user: User) -> Optional[PersistedUser]:
        return _FIXED_USER

    # -------------------------------------------------------------- threads

    async def list_threads(
        self, pagination: Pagination, filters: ThreadFilter
    ) -> PaginatedResponse[ThreadDict]:
        """Return threads from the LangGraph checkpoint, newest first.

        Pagination is cursor-based: the cursor is the index into the full list.
        Search filtering uses ThreadFilter.search against the thread title.
        """
        summaries = chat_store.thread_summaries()
        titles = chat_store.load_titles()

        # Apply search filter if present.
        search = (filters.search or "").lower().strip() if filters else ""
        if search:
            filtered = []
            for s in summaries:
                title = titles.get(s["id"], s["preview"] or "New chat").lower()
                if search in title or search in (s["preview"] or "").lower():
                    filtered.append(s)
            summaries = filtered

        # Cursor-based pagination: cursor is the string index of the start.
        start = 0
        if pagination.cursor:
            try:
                start = int(pagination.cursor)
            except (ValueError, TypeError):
                start = 0

        page_size = pagination.first
        page = summaries[start : start + page_size]
        has_next = (start + page_size) < len(summaries)

        threads: List[ThreadDict] = []
        for s in page:
            title = titles.get(s["id"])
            if not title:
                title = chat_store.fallback_title(s["preview"]) if s["preview"] else "New chat"

            updated = s.get("updated")
            created_at = updated.isoformat() if updated else "2024-01-01T00:00:00"

            threads.append(
                ThreadDict(
                    id=s["id"],
                    createdAt=created_at,
                    name=title,
                    userId=_FIXED_USER_ID,
                    userIdentifier=_FIXED_USER_IDENTIFIER,
                    tags=None,
                    metadata=None,
                    steps=self._build_steps_for_thread(s["id"]),
                    elements=None,
                )
            )

        end_cursor = str(start + len(page)) if has_next else None

        return PaginatedResponse(
            pageInfo=PageInfo(
                hasNextPage=has_next,
                startCursor=str(start),
                endCursor=end_cursor,
            ),
            data=threads,
        )

    def _build_steps_for_thread(self, thread_id: str) -> List[StepDict]:
        """Build lightweight StepDict entries for the sidebar preview.

        The sidebar only needs the first user message for preview text.
        Full history reconstruction happens in on_chat_resume, not here.
        """
        try:
            history = chat_store.load_history(thread_id)
        except Exception:
            return []

        steps: List[StepDict] = []
        for i, msg in enumerate(history):
            if msg["role"] == "user" and msg.get("type") == "text":
                steps.append(
                    StepDict(
                        name="user",
                        type="user_message",
                        id=str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{thread_id}-{i}")),
                        threadId=thread_id,
                        parentId=None,
                        command=None,
                        modes=None,
                        streaming=False,
                        waitForAnswer=None,
                        isError=None,
                        metadata={},
                        tags=None,
                        input="",
                        output=str(msg.get("content", "")),
                        createdAt=None,
                        start=None,
                        end=None,
                        generation=None,
                        showInput=None,
                        defaultOpen=None,
                        autoCollapse=None,
                        language=None,
                        icon=None,
                        feedback=None,
                    )
                )
            elif msg["role"] == "assistant" and msg.get("type") == "text":
                steps.append(
                    StepDict(
                        name="assistant",
                        type="assistant_message",
                        id=str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{thread_id}-{i}")),
                        threadId=thread_id,
                        parentId=None,
                        command=None,
                        modes=None,
                        streaming=False,
                        waitForAnswer=None,
                        isError=None,
                        metadata={},
                        tags=None,
                        input="",
                        output=str(msg.get("content", "")),
                        createdAt=None,
                        start=None,
                        end=None,
                        generation=None,
                        showInput=None,
                        defaultOpen=None,
                        autoCollapse=None,
                        language=None,
                        icon=None,
                        feedback=None,
                    )
                )
        return steps

    async def get_thread(self, thread_id: str) -> Optional[ThreadDict]:
        """Return a single thread for resume."""
        titles = chat_store.load_titles()
        title = titles.get(thread_id, "Chat")

        # Get the timestamp from summaries if possible.
        for s in chat_store.thread_summaries():
            if s["id"] == thread_id:
                updated = s.get("updated")
                created_at = updated.isoformat() if updated else "2024-01-01T00:00:00"
                break
        else:
            created_at = "2024-01-01T00:00:00"

        steps = self._build_steps_for_thread(thread_id)

        return ThreadDict(
            id=thread_id,
            createdAt=created_at,
            name=title,
            userId=_FIXED_USER_ID,
            userIdentifier=_FIXED_USER_IDENTIFIER,
            tags=None,
            metadata=None,
            steps=steps,
            elements=None,
        )

    async def get_thread_author(self, thread_id: str) -> str:
        return _FIXED_USER_IDENTIFIER

    async def delete_thread(self, thread_id: str):
        chat_store.delete_thread(thread_id)

    async def update_thread(
        self,
        thread_id: str,
        name: Optional[str] = None,
        user_id: Optional[str] = None,
        metadata: Optional[Dict] = None,
        tags: Optional[List[str]] = None,
    ):
        if name is not None:
            chat_store.save_title(thread_id, name)

    # ------------------------------------------------------------- feedback

    async def upsert_feedback(self, feedback: Feedback) -> str:
        """Map Chainlit's feedback to chat_store's (thread_id, index, value).

        Chainlit provides feedback.forId (the step/message ID) and
        feedback.threadId. We use the step ID to look up the message index
        within the thread's history. If we can't map it, we store under
        a synthetic index derived from the ID.
        """
        thread_id = feedback.threadId or "unknown"
        msg_index = self._step_id_to_index(thread_id, feedback.forId)
        chat_store.save_feedback(thread_id, msg_index, feedback.value)
        return feedback.id or str(uuid.uuid4())

    async def delete_feedback(self, feedback_id: str) -> bool:
        # We don't have a reverse lookup from feedback_id to (thread, index),
        # but the UI re-upserts with value toggled, so this is rarely called.
        return True

    def _step_id_to_index(self, thread_id: str, step_id: str) -> int:
        """Map a Chainlit step ID back to the message index in our history.

        Step IDs are generated as uuid5(NAMESPACE_DNS, "{thread_id}-{i}") in
        _build_steps_for_thread, so we can try to match.
        """
        try:
            history = chat_store.load_history(thread_id)
            for i, msg in enumerate(history):
                expected_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{thread_id}-{i}"))
                if expected_id == step_id:
                    return i
        except Exception:
            pass
        # Fallback: use hash of step_id mod a large number.
        return hash(step_id) % 10000

    # ------------------------------------------------ steps (no-ops: LangGraph persists)

    async def create_step(self, step_dict: StepDict):
        pass

    async def update_step(self, step_dict: StepDict):
        pass

    async def delete_step(self, step_id: str):
        pass

    # ---------------------------------------------- elements (no-ops)

    async def create_element(self, element: Element):
        pass

    async def get_element(
        self, thread_id: str, element_id: str
    ) -> Optional[ElementDict]:
        return None

    async def delete_element(self, element_id: str, thread_id: Optional[str] = None):
        pass

    # -------------------------------------------------- misc

    async def build_debug_url(self) -> str:
        return ""

    async def close(self) -> None:
        pass

    async def get_favorite_steps(self, user_id: str) -> List[StepDict]:
        return []
