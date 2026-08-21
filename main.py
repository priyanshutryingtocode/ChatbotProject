import streamlit as st

from chat_handler import OrderChatHandler
from database import get_messages, record_feedback
from sidebar import invalidate_recent_chats_cache, render_sidebar


# ---------------------------------------------------------
# PAGE CONFIG
# ---------------------------------------------------------

st.set_page_config(
    page_title="Order Status Assistant",
    page_icon="📦",
    layout="wide",
    initial_sidebar_state="collapsed",
)


# ---------------------------------------------------------
# STYLING
# ---------------------------------------------------------

def apply_theme() -> None:
    st.markdown(
        """
        <style>

        /* =========================
           PAGE
           ========================= */

        .stApp {
            background: #0f1117;
        }

        [data-testid="stHeader"] {
            display: none;
        }

        #MainMenu,
        footer {
            visibility: hidden;
        }


        /* =========================
           MAIN CONTENT
           ========================= */

        .block-container {
            max-width: 900px !important;
            margin: 0 auto !important;
            padding-top: 1.5rem !important;
            padding-bottom: 2rem !important;
        }


        /* =========================
           HEADER
           ========================= */

        .brand-title {
            font-size: 1.4rem;
            font-weight: 600;
            margin: 0;
        }


        /* =========================
           CHAT MESSAGES
           ========================= */

        [data-testid="stChatMessage"] {
            background: #1a1f2b;
            border: 1px solid #262c3a;
            border-radius: 12px;
            padding: 1rem 1.1rem;
            margin-bottom: 1rem;
        }


        /* =========================
           CHAT INPUT
           ========================= */

        [data-testid="stChatInput"] {
            max-width: 900px;
            margin: 0 auto;
            background: transparent !important;
            border: none !important;
            box-shadow: none !important;
        }

        [data-testid="stChatInput"] > div {
            background: transparent !important;
            border: none !important;
            box-shadow: none !important;
            padding: 0 !important;
        }

        [data-testid="stChatInput"] textarea,
        [data-testid="stChatInput"] [contenteditable="true"] {
            background: #1a1f2b !important;
            border: 1px solid #2a3140 !important;
            border-radius: 14px !important;
            padding: 0.75rem 1rem !important;
            box-shadow: none !important;
            outline: none !important;

            /* Single-line horizontal input */
            white-space: nowrap !important;
            overflow-x: auto !important;
            overflow-y: hidden !important;
            resize: none !important;

            /* Hide scrollbar */
            scrollbar-width: none !important;
        }

        [data-testid="stChatInput"] textarea::-webkit-scrollbar {
            display: none !important;
        }

        /* Keep the exact same appearance when focused */
        [data-testid="stChatInput"] textarea:focus,
        [data-testid="stChatInput"] textarea:focus-visible,
        [data-testid="stChatInput"]:focus-within,
        [data-testid="stChatInput"]:focus-within textarea {
            background: #1a1f2b !important;
            border: 1px solid #2a3140 !important;
            box-shadow: none !important;
            outline: none !important;
        }

        [data-testid="stChatInput"] textarea::placeholder {
            color: #6b7280;
        }


        /* =========================
           SEND BUTTON
           ========================= */

        [data-testid="stChatInputSubmitButton"] {
            background: transparent !important;
            border: none !important;
            box-shadow: none !important;
            outline: none !important;
            color: #9aa2b1 !important;
        }

        [data-testid="stChatInputSubmitButton"]:hover,
        [data-testid="stChatInputSubmitButton"]:focus,
        [data-testid="stChatInputSubmitButton"]:focus-visible,
        [data-testid="stChatInputSubmitButton"]:active {
            background: transparent !important;
            border: none !important;
            box-shadow: none !important;
            outline: none !important;
            color: #9aa2b1 !important;
        }

        [data-testid="stChatInputSubmitButton"][disabled] {
            background: transparent !important;
            border: none !important;
            box-shadow: none !important;
            color: #6b7280 !important;
        }


        /* =========================
           WELCOME SCREEN
           ========================= */

        .welcome {
            text-align: center;
            margin-top: 18vh;
            margin-bottom: 4vh;
        }

        .welcome-icon {
            font-size: 2.5rem;
            margin-bottom: 0.5rem;
        }

        .welcome-title {
            font-size: 1.25rem;
            font-weight: 600;
            color: #f0f2f6;
            margin-bottom: 0.5rem;
        }

        .welcome-text {
            color: #8b93a3;
            font-size: 0.95rem;
        }


        /* =========================
           SIDEBAR
           ========================= */

        [data-testid="stSidebar"] {
            border-right: 1px solid rgba(255, 255, 255, 0.06);
        }

        [data-testid="stSidebarContent"] {
            padding-top: 1rem;
        }

        [data-testid="stSidebar"] h2,
        [data-testid="stSidebar"] h3 {
            font-size: 0.78rem;
            letter-spacing: 0.08em;
            text-transform: uppercase;
            color: #8b93a3;
            margin: 0 0 0.35rem;
        }

        [data-testid="stSidebar"] [data-testid="stTextInput"] input,
        [data-testid="stSidebar"] [data-testid="stNumberInput"] input {
            font-size: 0.85rem;
            padding: 0.3rem 0.5rem;
        }

        </style>
        """,
        unsafe_allow_html=True,
    )


# ---------------------------------------------------------
# SESSION STATE
# ---------------------------------------------------------

FIRST_LOAD = "UNSET"


def _load_conversation(conversation_id: str) -> None:
    """Load an existing conversation."""

    st.session_state.viewing_conversation_id = conversation_id

    st.session_state.chat_handler = OrderChatHandler(
        conversation_id=conversation_id
    )

    st.session_state.messages = [
        {
            "role": (
                message.get("role")
                if message.get("role") in ("user", "assistant")
                else "assistant"
            ),
            "content": message.get("content", ""),
        }
        for message in get_messages(conversation_id)
        if message.get("content")
    ]

    st.query_params["chat"] = conversation_id


def initialize_session_state() -> None:

    if "messages" not in st.session_state:
        st.session_state.messages = []

    if "last_db_results" not in st.session_state:
        st.session_state.last_db_results = {}

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
                st.session_state.chat_handler = OrderChatHandler()

    elif viewing is not None:

        handler = st.session_state.get("chat_handler")

        if (
            handler is None
            or handler.conversation_id != viewing
        ):
            _load_conversation(viewing)


# ---------------------------------------------------------
# NEW CHAT
# ---------------------------------------------------------

def start_new_chat() -> None:

    st.session_state.viewing_conversation_id = None

    st.session_state.chat_handler.end_session()

    st.session_state.messages = []

    st.session_state.last_db_results = {}

    st.session_state.chat_handler.clear_context()

    st.session_state.pop(
        "feedback_given",
        None,
    )

    st.session_state.pop(
        "recent_chat_choice",
        None,
    )

    if "chat" in st.query_params:
        del st.query_params["chat"]


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
            "### 📦 Order Support"
        )

    with right:

        st.button(
            "New chat",
            on_click=start_new_chat,
            use_container_width=True,
        )


# ---------------------------------------------------------
# FEEDBACK
# ---------------------------------------------------------

def render_feedback_buttons(message_index: int) -> None:

    if st.session_state.get("feedback_given"):

        st.caption("Thanks for your feedback!")

        return

    left, right, _ = st.columns(
        [1, 1, 6]
    )

    with left:

        if st.button(
            "👍",
            key=f"fb_up_{message_index}",
            help="Helpful",
        ):

            message_id = (
                st.session_state
                .chat_handler
                .last_message_ids
                .get("assistant", "")
            )

            record_feedback(
                message_id,
                "up",
            )

            st.session_state.feedback_given = True

            st.rerun()

    with right:

        if st.button(
            "👎",
            key=f"fb_down_{message_index}",
            help="Not helpful",
        ):

            message_id = (
                st.session_state
                .chat_handler
                .last_message_ids
                .get("assistant", "")
            )

            record_feedback(
                message_id,
                "down",
            )

            st.session_state.feedback_given = True

            st.rerun()


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
                <div class="welcome-icon">📦</div>
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

            st.markdown(
                message["content"]
            )

    # -------------------------
    # Input
    # -------------------------

    prompt = st.chat_input(
        "Ask about an order, customer, delivery, or payment status..."
    )

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

            response = st.write_stream(
                st.session_state.chat_handler.stream_response(
                    prompt
                )
            )

        except Exception:

            response = (
                "I couldn't complete that lookup. "
                "Please verify the search value and try again."
            )

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

    st.session_state.pop(
        "feedback_given",
        None,
    )

    invalidate_recent_chats_cache()

    st.rerun()


# ---------------------------------------------------------
# MAIN
# ---------------------------------------------------------

def main() -> None:

    apply_theme()

    initialize_session_state()

    render_sidebar()

    render_header()

    render_chat_interface()


if __name__ == "__main__":
    main()