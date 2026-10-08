import logging
from pathlib import Path
from uuid import uuid4

import streamlit as st

from orderbot.chat import OrderChatHandler
from orderbot.database import conversation_belongs_to_session, get_messages
from orderbot.sidebar import invalidate_recent_chats_cache, render_sidebar

logger = logging.getLogger(__name__)

# ---------------------------------------------------------
# PAGE CONFIG
# ---------------------------------------------------------

st.set_page_config(
    page_title="Order Status Assistant",
    page_icon="O",
    layout="wide",
    initial_sidebar_state="collapsed",
)


# ---------------------------------------------------------
# STYLING
# ---------------------------------------------------------

_CSS_PATH = Path(__file__).parent / "static" / "app.css"


@st.cache_data(show_spinner=False)
def _load_css() -> str:
    return _CSS_PATH.read_text(encoding="utf-8")


def apply_theme() -> None:
    """Inject the app stylesheet. Kept in static/app.css so main.py stays readable."""
    st.markdown(f"<style>{_load_css()}</style>", unsafe_allow_html=True)


# ---------------------------------------------------------
# SESSION STATE
# ---------------------------------------------------------

FIRST_LOAD = "UNSET"


def current_session_id() -> str:
    """Stable id for this browser session, created on first use.

    Used to scope stored conversations so one visitor cannot list or reload
    another's (migration 006). This is a browser session, not a person: it stops
    cross-visitor reads, it is not authentication.
    """
    session_id = st.session_state.get("session_id")

    if not session_id:
        session_id = str(uuid4())
        st.session_state.session_id = session_id

    return session_id


def _start_new_handler() -> None:
    st.session_state.chat_handler = OrderChatHandler(session_id=current_session_id())


def _load_conversation(conversation_id: str) -> bool:
    """Load a conversation this browser session owns. False if it does not.

    Every entry point funnels through here — the recent-chats radio and the
    `?chat=` query parameter — so the ownership check covers both. A rejected id
    falls back to a fresh chat rather than raising.
    """
    if not conversation_belongs_to_session(conversation_id, current_session_id()):
        logger.warning("Rejected load of conversation not owned by this session")
        st.session_state.viewing_conversation_id = None
        st.session_state.messages = []
        _start_new_handler()
        st.query_params.pop("chat", None)
        return False

    st.session_state.viewing_conversation_id = conversation_id

    st.session_state.chat_handler = OrderChatHandler(
        conversation_id=conversation_id,
        session_id=current_session_id(),
    )

    st.session_state.messages = [
        {
            "role": (message.get("role") if message.get("role") in ("user", "assistant") else "assistant"),
            "content": message.get("content", ""),
        }
        for message in get_messages(conversation_id)
        if message.get("content")
    ]

    st.query_params["chat"] = conversation_id

    return True


def initialize_session_state() -> None:
    if "messages" not in st.session_state:
        st.session_state.messages = []

    if "viewing_conversation_id" not in st.session_state:
        st.session_state.viewing_conversation_id = FIRST_LOAD

    if "chat_handler" not in st.session_state:
        st.session_state.chat_handler = None

    requested = st.session_state.pop(
        "resume_conversation_id",
        None,
    )

    viewing = st.session_state.viewing_conversation_id

    if requested:
        _load_conversation(requested)

    elif viewing == FIRST_LOAD:
        url_id = st.query_params.get("chat")

        if url_id:
            _load_conversation(url_id)

        else:
            st.session_state.viewing_conversation_id = None

            if st.session_state.chat_handler is None:
                _start_new_handler()

    elif viewing is not None:
        handler = st.session_state.get("chat_handler")

        if handler is None or handler.conversation_id != viewing:
            _load_conversation(viewing)


# ---------------------------------------------------------
# NEW CHAT
# ---------------------------------------------------------


def start_new_chat() -> None:
    st.session_state.viewing_conversation_id = None

    st.session_state.chat_handler.end_session()

    st.session_state.messages = []

    st.session_state.chat_handler.clear_context()

    st.session_state.pop(
        "recent_chat_choice",
        None,
    )

    st.query_params.pop("chat", None)


# ---------------------------------------------------------
# HEADER
# ---------------------------------------------------------


def render_header() -> None:
    left, right = st.columns(
        [4, 1],
        vertical_alignment="center",
    )

    with left:
        st.markdown(
            "### Order Status Assistant",
        )

    with right:
        st.button(
            "New chat",
            on_click=start_new_chat,
            use_container_width=True,
        )


# ---------------------------------------------------------
# CHAT
# ---------------------------------------------------------


def render_chat_interface() -> None:
    # -------------------------
    # Welcome screen
    # -------------------------

    if not st.session_state.messages:
        st.markdown(
            """
            <div class="welcome">
                <div class="welcome-icon">Order support</div>
                <div class="welcome-title">
                    Track any order in seconds
                </div>
                <div class="welcome-text">
                    Enter an order number with an email, phone number, or name.
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )

    # -------------------------
    # Existing messages
    # -------------------------

    for message in st.session_state.messages:
        with st.chat_message(message["role"]):
            st.markdown(message["content"])

    # -------------------------
    # Input
    # -------------------------

    prompt = st.chat_input("Ask about an order, customer, delivery, or payment status...")

    if not prompt:
        return

    # -------------------------
    # User message
    # -------------------------

    st.session_state.messages.append(
        {
            "role": "user",
            "content": prompt,
        }
    )

    with st.chat_message("user"):
        st.markdown(prompt)

    # -------------------------
    # Assistant response
    # -------------------------

    with st.chat_message("assistant"):
        try:
            response = st.write_stream(st.session_state.chat_handler.stream_response(prompt))

        except Exception:
            # The handler already degrades internally and does not raise, so this
            # is a last-resort net. Log the traceback: without it a failure here
            # is indistinguishable from the user mistyping a lookup value.
            logger.exception("Chat response failed")
            response = "I couldn't complete that lookup. Please verify the search value and try again."

            st.markdown(response)

    # -------------------------
    # Save response
    # -------------------------

    st.session_state.messages.append(
        {
            "role": "assistant",
            "content": response,
        }
    )

    invalidate_recent_chats_cache()

    st.rerun()


# ---------------------------------------------------------
# MAIN
# ---------------------------------------------------------


def main() -> None:
    apply_theme()

    session_id = current_session_id()

    initialize_session_state()

    render_sidebar(session_id)

    render_header()

    render_chat_interface()


if __name__ == "__main__":
    main()
