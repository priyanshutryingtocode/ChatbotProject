import streamlit as st

from chat_handler import OrderChatHandler
from database import format_order_for_display, format_order_number, format_timestamp, record_feedback
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


def initialize_session_state() -> None:
    if "messages" not in st.session_state:
        st.session_state.messages = []
    if "last_db_results" not in st.session_state:
        st.session_state.last_db_results = {}
    if "chat_handler" not in st.session_state:
        st.session_state.chat_handler = OrderChatHandler()


def start_new_chat() -> None:
    st.session_state.chat_handler.end_session()
    st.session_state.messages = []
    st.session_state.last_db_results = {}
    st.session_state.chat_handler.clear_context()
    st.session_state.pop("feedback_given", None)


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
