import streamlit as st

from chat_handler import OrderChatHandler
from database import get_messages, record_feedback
from sidebar import invalidate_recent_chats_cache, render_sidebar


st.set_page_config(page_title="Order Status Assistant", page_icon="📦", layout="wide", initial_sidebar_state="expanded")


def apply_theme() -> None:
    st.markdown(
        """
        <style>
            /* ── One-page lock ────────────────────────────────────────────
               The browser never scrolls; individual panes own their own
               scrollbars (chat transcript, sidebar overflow). */
            .stApp { height: 100vh; overflow: hidden; }
            [data-testid="stHeader"] { display: none; }
            #MainMenu, footer { visibility: hidden; }

            [data-testid="stMain"],
            [data-testid="stMainBlockContainer"] {
                height: 100vh;
                overflow: hidden;
                display: flex;
                flex-direction: column;
            }

            .block-container {
                max-width: 1400px;
                padding: .5rem 1.25rem 0;
                display: flex;
                flex-direction: column;
                flex: 1;
                min-height: 0;
            }

            /* ── Centered reading column ─────────────────────────────────
               Header, transcript, status hints and input share one spine. */
            .block-container [data-testid="stElementContainer"],
            .block-container [data-testid="stHorizontalBlock"],
            .block-container [data-testid="stVerticalBlock"],
            .block-container [data-testid="stVerticalBlockBorderless"] {
                max-width: 840px;
                width: 100%;
                margin-inline: auto;
            }

            /* Transcript pane fills whatever vertical space remains.
               Reserve ≈ header row + chat input + paddings (~150px).
               !important overrides the inline height Streamlit sets. */
            .block-container [data-testid="stVerticalBlockBorderless"] {
                height: calc(100vh - 150px) !important;
                max-height: none;
            }

            /* ── Chat surfaces ─────────────────────────────────────────── */
            [data-testid="stChatMessage"] {
                background: #f6f7f9;
                border: 1px solid #e7e9ee;
                border-radius: 12px;
                padding: .65rem .9rem;
            }
            [data-testid="stChatMessage"] [data-testid="stMarkdownContainer"] p:last-child { margin-bottom: 0; }

            [data-testid="stChatInput"] textarea,
            [data-testid="stChatInput"] [contenteditable="true"] {
                background: #fff;
                border: 1px solid #dfe3ea;
                border-radius: 999px;
                box-shadow: 0 1px 2px rgba(16, 24, 40, .05);
            }
            [data-testid="stChatInput"]:focus-within textarea,
            [data-testid="stChatInput"]:focus-within [contenteditable="true"] {
                border-color: #5c6ac4;
                box-shadow: 0 0 0 3px rgba(92, 106, 196, .15);
            }

            /* Slim scrollbar on the transcript only */
            .block-container [data-testid="stVerticalBlockBorderless"]::-webkit-scrollbar { width: 6px; }
            .block-container [data-testid="stVerticalBlockBorderless"]::-webkit-scrollbar-thumb {
                background: rgba(16, 24, 40, .18);
                border-radius: 999px;
            }

            /* ── Sidebar micro-pass ────────────────────────────────────── */
            [data-testid="stSidebar"] {
                border-right: 1px solid rgba(49, 51, 63, 0.15);
                min-width: 340px;
            }
            [data-testid="stSidebarContent"] { overflow-y: auto; padding-top: 1rem; }
            [data-testid="stSidebar"] h2,
            [data-testid="stSidebar"] h3 {
                font-size: .78rem;
                letter-spacing: .08em;
                text-transform: uppercase;
                color: #5f6368;
                margin: 0 0 .35rem;
            }
            [data-testid="stSidebar"] [data-testid="stTextInput"] input,
            [data-testid="stSidebar"] [data-testid="stNumberInput"] input {
                font-size: .85rem;
                padding: .3rem .5rem;
            }

            .brand-row h1 { font-size: 1.35rem; margin: 0; line-height: 1.15; }
            .lookup-panel-title { font-size: 1rem; margin: 0; }

            /* ── Empty-state hero ──────────────────────────────────────── */
            .chat-hero { text-align: center; padding-top: 14vh; color: #444; }
            .hero-mark { font-size: 2rem; }
            .hero-title { font-size: 1.05rem; font-weight: 600; margin: .35rem 0 .6rem; color: #202124; }
            .kbd {
                background: #eef0f4;
                border: 1px solid #dde1e8;
                border-radius: 6px;
                padding: .1rem .45rem;
                font-size: .85em;
                white-space: nowrap;
            }
            .hint-line, .example-line { color: #5f6368; font-size: .9rem; margin-top: .35rem; }
        </style>
        """,
        unsafe_allow_html=True,
    )


# Sentinel: this browser session has not decided which conversation to view
# yet. The URL ?chat= parameter is honored only while the sentinel is set, so
# a stale URL can never resurrect a conversation after an explicit "New chat".
FIRST_LOAD = "UNSET"

# Chat transcript renders inside a scrollable pane so the input stays pinned
# below it while messages stream in. Pre-CSS fallback only: apply_theme()'s
# stylesheet overrides this height responsively (calc(100vh - 150px)) so the
# transcript always fills exactly the remaining viewport.
TRANSCRIPT_HEIGHT = 400


def _load_conversation(conversation_id: str) -> None:
    """Hydrate handler and transcript around an existing conversation."""
    st.session_state.viewing_conversation_id = conversation_id
    st.session_state.chat_handler = OrderChatHandler(conversation_id=conversation_id)
    st.session_state.messages = [
        {"role": message.get("role") if message.get("role") in ("user", "assistant") else "assistant",
         "content": message.get("content", "")}
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

    # Sidebar selection wins; the URL ?chat= param counts only on first load.
    requested = st.session_state.pop("resume_conversation_id", None)
    viewing = st.session_state.viewing_conversation_id

    if requested:
        _load_conversation(requested)
    elif viewing == FIRST_LOAD:
        url_id = st.query_params.get("chat")
        if url_id:
            _load_conversation(url_id)
        else:
            st.session_state.viewing_conversation_id = None
            if st.session_state.get("chat_handler") is None:
                st.session_state.chat_handler = OrderChatHandler()
    elif viewing is not None:
        handler = st.session_state.get("chat_handler")
        if handler is None or handler.conversation_id != viewing:
            _load_conversation(viewing)


def start_new_chat() -> None:
    # Authoritative fresh-start flag; must be set before anything can rerun so
    # initialize_session_state ignores any lingering ?chat= URL param.
    st.session_state.viewing_conversation_id = None
    st.session_state.chat_handler.end_session()
    st.session_state.messages = []
    st.session_state.last_db_results = {}
    st.session_state.chat_handler.clear_context()
    st.session_state.pop("feedback_given", None)
    # Reset the sidebar picker to its neutral placeholder so it stops pointing
    # at the conversation we just left.
    st.session_state.pop("recent_chat_choice", None)
    if "chat" in st.query_params:
        del st.query_params["chat"]


def render_header() -> None:
    left, right = st.columns([4, 1], vertical_alignment="center")
    with left:
        st.markdown('<div class="brand-row"><h1>Order Support</h1></div>', unsafe_allow_html=True)
    with right:
        st.button("New chat", on_click=start_new_chat, use_container_width=True)


def _rerun_app() -> None:
    """Rerun the whole app whether or not fragments are available."""
    if hasattr(st, "fragment"):
        st.rerun(scope="app")
    else:
        st.rerun()


def render_feedback_buttons(message_index: int) -> None:
    if st.session_state.get("feedback_given"):
        st.caption("Thanks for your feedback!")
        return

    left, right = st.columns([1, 1])
    with left:
        if st.button("👍 Helpful", key=f"fb_up_{message_index}"):
            message_id = st.session_state.chat_handler.last_message_ids.get("assistant", "")
            record_feedback(message_id, "up")
            st.session_state.feedback_given = True
            st.rerun()
    with right:
        if st.button("👎 Not helpful", key=f"fb_down_{message_index}"):
            message_id = st.session_state.chat_handler.last_message_ids.get("assistant", "")
            record_feedback(message_id, "down")
            st.session_state.feedback_given = True
            st.rerun()


def render_chat_interface() -> None:
    prompt = st.chat_input("Ask about an order, customer, delivery, or payment status…")
    if prompt and not st.session_state.pop("pending_stream", None):
        # Phase 1: stash the prompt and normalize instantly so the input snaps
        # back beneath the transcript before any slow work starts.
        st.session_state.pending_stream = prompt
        _rerun_app()

    pending = st.session_state.pop("pending_stream", None)

    # Transcript lives in a fixed-height scrollable pane; the input stays
    # pinned below it while messages arrive.
    with st.container(height=TRANSCRIPT_HEIGHT):
        if not st.session_state.messages and not pending:
            st.markdown(
                """
                <div class="chat-hero">
                  <div class="hero-mark">📦</div>
                  <div class="hero-title">Track any order in seconds</div>
                  <div class="hint-line">Two details unlock an order:</div>
                  <div><span class="kbd">order number</span> plus
                       <span class="kbd">email</span> / <span class="kbd">phone</span> /
                       <span class="kbd">name</span></div>
                  <div class="example-line">try&nbsp;<span class="kbd">check order 42, email you@example.com</span></div>
                </div>
                """,
                unsafe_allow_html=True,
            )

        for message in st.session_state.messages:
            with st.chat_message(message["role"]):
                st.markdown(message["content"])

        if pending:
            st.session_state.messages.append({"role": "user", "content": pending})
            with st.chat_message("user"):
                st.markdown(pending)

            handler = st.session_state.chat_handler
            status_slot = st.empty()

            def report_status(step: str) -> None:
                status_slot.caption(step)

            with st.chat_message("assistant"):
                try:
                    full_response = st.write_stream(handler.stream_response(pending, on_step=report_status))
                except Exception:
                    # stream_response already guards internally; last resort only.
                    full_response = "I couldn't complete that lookup. Please verify the search value and try again."
                    st.markdown(full_response)
                finally:
                    status_slot.empty()

            st.session_state.messages.append({"role": "assistant", "content": full_response})
            invalidate_recent_chats_cache()
            _rerun_app()


if hasattr(st, "fragment"):
    render_chat_interface = st.fragment(render_chat_interface)


def main() -> None:
    apply_theme()
    initialize_session_state()
    render_sidebar()
    render_header()
    render_chat_interface()


if __name__ == "__main__":
    main()
