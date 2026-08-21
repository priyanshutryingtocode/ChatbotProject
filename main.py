import time

import streamlit as st

from chat_handler import OrderChatHandler
from database import (
    format_event_line,
    format_order_for_display,
    format_order_number,
    format_timestamp,
    get_messages,
    get_order_timeline,
    record_feedback,
)
from sidebar import render_sidebar


st.set_page_config(page_title="Order Status Assistant", page_icon="📦", layout="wide", initial_sidebar_state="expanded")


def apply_theme() -> None:
    st.markdown(
        """
        <style>
            .block-container { max-width: 1240px; padding: 2.75rem 3rem 3rem; }
            [data-testid="stSidebar"] { border-right: 1px solid rgba(49, 51, 63, 0.15); min-width: 340px; }
            [data-testid="stSidebar"] > div:first-child { padding-top: 1.5rem; }
            [data-testid="stChatMessage"] { border-radius: 14px; }
            .support-eyebrow { color: #5c6ac4; font-size: 0.85rem; font-weight: 700; letter-spacing: 0.08em; text-transform: uppercase; }
            .support-subtitle { color: #5f6368; font-size: 1.05rem; margin-bottom: 1.25rem; }
            .lookup-panel-title { margin-bottom: 0; }
        </style>
        """,
        unsafe_allow_html=True,
    )


def typewriter(text: str, word_delay: float = 0.02):
    """Yield a response word by word so replies render progressively."""
    words = text.split(" ")
    for index, word in enumerate(words):
        yield word + (" " if index < len(words) - 1 else "")
        if index < len(words) - 1:
            time.sleep(word_delay)


# Sentinel: this browser session has not decided which conversation to view
# yet. The URL ?chat= parameter is honored only while the sentinel is set, so
# a stale URL can never resurrect a conversation after an explicit "New chat".
FIRST_LOAD = "UNSET"


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
    st.session_state.streamed_index = max(len(st.session_state.messages) - 1, -1)
    st.query_params["chat"] = conversation_id


def initialize_session_state() -> None:
    if "messages" not in st.session_state:
        st.session_state.messages = []
    if "last_db_results" not in st.session_state:
        st.session_state.last_db_results = {}
    if "streamed_index" not in st.session_state:
        st.session_state.streamed_index = -1
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
    st.session_state.streamed_index = -1
    st.session_state.pop("feedback_given", None)
    # Reset the sidebar picker to its neutral placeholder so it stops pointing
    # at the conversation we just left.
    st.session_state.pop("recent_chat_choice", None)
    if "chat" in st.query_params:
        del st.query_params["chat"]


def render_header() -> None:
    left, right = st.columns([5, 1])
    with left:
        st.title("Order Support")
        st.markdown(
            '<div class="support-subtitle">Look up live order details or ask the assistant for a clear customer-ready update.</div>',
            unsafe_allow_html=True,
        )
    with right:
        st.write("")
        st.write("")
        st.button("New chat", on_click=start_new_chat, use_container_width=True)


def handle_user_input(user_input: str) -> None:
    st.session_state.messages.append({"role": "user", "content": user_input})
    st.session_state.pop("feedback_given", None)
    try:
        with st.spinner("Checking order details..."):
            ai_response, db_results = st.session_state.chat_handler.process_user_message(user_input)
        if db_results:
            st.session_state.last_db_results = db_results
        st.session_state.messages.append({"role": "assistant", "content": ai_response})
    except Exception:
        st.session_state.messages.append(
            {"role": "assistant", "content": "I couldn't complete that lookup. Please verify the search value and try again."}
        )


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
    if not st.session_state.messages:
        st.info("I need two details to verify an order: your order number plus your email, phone number, or name. Example: `check order 42, email you@example.com`")

    for index, message in enumerate(st.session_state.messages):
        with st.chat_message(message["role"]):
            is_latest_assistant = message["role"] == "assistant" and index == len(st.session_state.messages) - 1
            if is_latest_assistant and index > st.session_state.streamed_index:
                st.write_stream(typewriter(message["content"]))
                st.session_state.streamed_index = index
            else:
                st.markdown(message["content"])
            if message["role"] == "assistant" and index == len(st.session_state.messages) - 1:
                render_feedback_buttons(index)

    prompt = st.chat_input("Ask about an order, customer, delivery, or payment status…")
    if prompt:
        handle_user_input(prompt)
        st.rerun()


def render_manual_lookup_results() -> None:
    if "manual_lookup_results" not in st.session_state:
        return

    results = st.session_state.manual_lookup_results
    label = st.session_state.get("manual_lookup_label", "Manual lookup")
    st.divider()
    if not results:
        st.warning(f"No orders found for {label}. Try a different lookup value.")
        return

    title_col, action_col = st.columns([5, 1])
    with title_col:
        st.markdown('<h3 class="lookup-panel-title">Manual lookup results</h3>', unsafe_allow_html=True)
        st.caption(f"{label} · {len(results)} matching order{'s' if len(results) != 1 else ''}")
    with action_col:
        if st.button("Clear results", use_container_width=True, key="clear_main_lookup"):
            st.session_state.pop("manual_lookup_results", None)
            st.session_state.pop("manual_lookup_label", None)
            st.rerun()

    for order in results:
        shipment = (order.get("shipments") or [{}])[0]
        customer = order.get("customers") or {}
        with st.container(border=True):
            header, status_column, total_column = st.columns([3, 2, 2])
            with header:
                st.subheader(f"Order #{format_order_number(order.get('public_order_id'))}")
                st.caption(customer.get("full_name", "Customer unavailable"))
            with status_column:
                st.metric("Status", order.get("status", "Unknown"))
            with total_column:
                amount = float(order.get("total_amount") or 0)
                st.metric("Order total", f"{order.get('currency', 'USD')} {amount:,.2f}")

            delivery = shipment.get("delivered_at") or shipment.get("estimated_delivery_at")
            if delivery:
                delivery_label = "Delivered" if shipment.get("delivered_at") else "Estimated delivery"
                st.caption(f"{delivery_label}: {format_timestamp(delivery)}")
            with st.expander("View complete order details"):
                st.markdown(format_order_for_display(order))
                timeline = get_order_timeline(order)
                if timeline:
                    st.markdown("**Event timeline**")
                    for event in timeline:
                        st.markdown(f"- {format_event_line(event)}")


def main() -> None:
    apply_theme()
    initialize_session_state()
    render_sidebar()
    render_header()
    render_manual_lookup_results()
    render_chat_interface()
    st.divider()
    st.caption("For order changes, cancellations, or address updates, follow your support-team process.")


if __name__ == "__main__":
    main()
