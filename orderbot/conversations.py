"""Conversation loading and privacy-filtered persistence for the chat service."""

import logging

from .database import append_message, create_conversation, end_conversation, get_messages
from .privacy import persisted_message

logger = logging.getLogger(__name__)


class ConversationStore:
    """Small adapter around transcript persistence; it stores no verification state."""

    def __init__(self, conversation_id: str | None, session_id: str | None):
        self.conversation_id = conversation_id
        self.session_id = session_id

    def load(self, stored_messages: list[dict] | None = None) -> list[tuple[str, str]]:
        if self.conversation_id and stored_messages is None:
            try:
                stored_messages = get_messages(self.conversation_id, limit=50)
            except Exception:
                logger.exception("Failed to load conversation history")
                stored_messages = []
        return [
            ("human" if item.get("role") == "user" else "assistant", item.get("content", ""))
            for item in (stored_messages or [])
            if item.get("content")
        ]

    def append(self, role: str, content: str, order_context: bool = False) -> None:
        if not self.conversation_id:
            self.conversation_id = create_conversation(session_id=self.session_id)
        if not self.conversation_id:
            return
        append_message(
            self.conversation_id,
            "user" if role == "human" else role,
            persisted_message(role, content, order_context=order_context),
        )

    def end(self) -> None:
        if self.conversation_id:
            end_conversation(self.conversation_id)
            self.conversation_id = None
