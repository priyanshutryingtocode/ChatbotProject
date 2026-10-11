import logging
from pathlib import Path
from uuid import uuid4

import streamlit as st

from orderbot.chat import OrderChatHandler
from orderbot.database import conversation_belongs_to_session, format_timestamp, get_messages, get_order_timeline, order_summary
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

    stored_messages = get_messages(conversation_id)
    st.session_state.chat_handler = OrderChatHandler(
        conversation_id=conversation_id,
        session_id=current_session_id(),
        stored_messages=stored_messages,
    )

    st.session_state.messages = [
        {
            "role": (message.get("role") if message.get("role") in ("user", "assistant") else "assistant"),
            "content": message.get("content", ""),
        }
        for message in stored_messages
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
    brand, actions = st.columns([5, 1], vertical_alignment="center")
    with brand:
        st.markdown(
            """
            <div class="app-brand">
                <div class="brand-mark">OS</div>
                <div><div class="brand-name">Order support</div>
                <div class="brand-caption">Order help, clearly handled</div></div>
            </div>
            """,
            unsafe_allow_html=True,
        )
    with actions:
        st.button("New conversation", on_click=start_new_chat, use_container_width=True)
    st.markdown('<div class="app-header-rule"></div>', unsafe_allow_html=True)


def _queue_prompt(prompt: str) -> None:
    st.session_state.suggested_prompt = prompt


def render_welcome() -> None:
    st.markdown(
        """
        <section class="welcome-panel">
            <div class="welcome-eyebrow">CUSTOMER CARE</div>
            <div class="welcome-title">How can we help with your order?</div>
            <div class="welcome-text">Get a verified update, find an answer in our help policies,
            or tell us what went wrong. Your order details stay private.</div>
        </section>
        """,
        unsafe_allow_html=True,
    )
    prompts = [
        ("01", "Check an order", "Status, delivery estimate, or tracking", "I want to check an order", "Start order check"),
        ("02", "Delivery issue", "A delivery is late or marked delivered", "I have a problem with my delivery", "Get delivery help"),
        ("03", "Returns & refunds", "Find the relevant policy", "What is your return and refund policy?", "Browse policies"),
    ]
    columns = st.columns(3, gap="medium")
    for column, (icon, title, description, prompt, action_label) in zip(columns, prompts):
        with column:
            with st.container(border=True):
                st.markdown(f'<div class="quick-icon">{icon}</div>', unsafe_allow_html=True)
                st.markdown(f"**{title}**")
                st.caption(description)
                st.button(
                    action_label,
                    key=f"quick-start-{title}",
                    use_container_width=True,
                    on_click=_queue_prompt,
                    args=(prompt,),
                )
    st.caption("For an order lookup, we’ll ask for the order number and a matching email, phone number, or name.")


def render_verified_order() -> None:
    handler = st.session_state.chat_handler
    orders = (handler.last_db_results or {}).get("matched") or []
    if not orders:
        return

    st.markdown("#### Your verified order" if len(orders) == 1 else "#### Matching orders")
    for order in orders:
        summary = order_summary(order)
        shipment_time = summary["delivered_at"] or summary["estimated_delivery_at"]
        shipment_label = "Delivered" if summary["delivered_at"] else "Estimated delivery"

        with st.container(border=True):
            identity_col, status_col, delivery_col = st.columns([1.3, 1, 1.4], vertical_alignment="center")
            with identity_col:
                st.caption("ORDER")
                st.markdown(f"### #{summary['order_number']}")
            with status_col:
                st.caption("CURRENT STATUS")
                st.markdown(f"**{summary['status']}**")
            with delivery_col:
                st.caption(shipment_label.upper() if shipment_time else "DELIVERY")
                st.markdown(f"**{format_timestamp(shipment_time) if shipment_time else 'No estimate available'}**")

            if not handler.name_only_verified:
                timeline = get_order_timeline(order)
                if timeline:
                    with st.expander(f"View activity for order #{summary['order_number']}"):
                        for event in timeline[-4:]:
                            event_label = (event.get("event_type") or "Update").replace("_", " ").title()
                            st.markdown(f"**{event_label}** · {format_timestamp(event.get('event_at'))}")


def render_pending_action() -> None:
    pending_action = st.session_state.chat_handler.pending_action
    if not pending_action:
        return

    action_label = pending_action["action"].replace("_request", "").replace("_", " ").title()
    st.markdown("#### Review your support request")
    with st.container(border=True):
        st.markdown(f"**{action_label} · Order #{pending_action['order_id']:04d}**")
        st.caption("This submits a request for a support agent to review. It does not change your order.")
        st.markdown(f"**Reason**\n\n{pending_action['reason']}")
        expires_at = pending_action["expires_at"].astimezone()
        st.caption(f"Confirmation expires at {expires_at.strftime('%I:%M %p').lstrip('0')} local time.")
        confirm_col, cancel_col = st.columns(2)
        with confirm_col:
            if st.button(
                "Submit support request",
                key=f"confirm-action-{pending_action['request_key']}",
                type="primary",
                use_container_width=True,
            ):
                submitted, message = st.session_state.chat_handler.confirm_pending_action(
                    pending_action["request_key"]
                )
                st.session_state.messages.append({"role": "assistant", "content": message})
                (st.success if submitted else st.error)(message)
                invalidate_recent_chats_cache()
                st.rerun()
        with cancel_col:
            if st.button(
                "Discard request",
                key=f"cancel-action-{pending_action['request_key']}",
                use_container_width=True,
            ):
                st.session_state.chat_handler.discard_pending_action(pending_action["request_key"])
                st.info("Request discarded.")
                st.rerun()


# ---------------------------------------------------------
# CHAT
# ---------------------------------------------------------


def render_chat_interface() -> None:
    # -------------------------
    # Welcome screen
    # -------------------------

    if not st.session_state.messages:
        render_welcome()

    render_verified_order()

    # -------------------------
    # Existing messages
    # -------------------------

    for message in st.session_state.messages:
        avatar = ":material/support_agent:" if message["role"] == "assistant" else ":material/person:"
        with st.chat_message(message["role"], avatar=avatar):
            st.markdown(message["content"])

    render_pending_action()

    # -------------------------
    # Input
    # -------------------------

    prompt = st.chat_input("Ask about an order or how we can help...")
    if not prompt:
        prompt = st.session_state.pop("suggested_prompt", None)
    st.caption("We keep a redacted chat transcript to resume conversations. Order and identity details are removed from saved messages.")

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

    with st.chat_message("user", avatar=":material/person:"):
        st.markdown(prompt)

    # -------------------------
    # Assistant response
    # -------------------------

    with st.chat_message("assistant", avatar=":material/support_agent:"):
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
