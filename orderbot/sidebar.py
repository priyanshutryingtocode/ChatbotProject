"""Sidebar controls: manual two-field order lookup, results panel, recent chats."""

import re

import streamlit as st

from .config import MAX_ORDER_ID, MIN_ORDER_ID
from .database import (
    format_event_line,
    format_order_for_display,
    format_timestamp,
    get_order_timeline,
    list_conversations,
    normalize_phone,
    order_summary,
)
from .orders import OrderCriteria, OrderLookup, lookup_order


@st.cache_data(ttl=20, show_spinner=False)
def _cached_recent_chats(session_id: str, limit: int = 10):
    """Brief cache so the sidebar stops querying Supabase on every rerun.

    `session_id` is part of the cache key on purpose. st.cache_data is scoped to
    the process, not the session, so without it one visitor's conversations
    would be served to the next.
    """
    return list_conversations(limit, session_id=session_id)


def invalidate_recent_chats_cache() -> None:
    """Called by the chat flow after each exchange so fresh sessions appear."""
    _cached_recent_chats.clear()


def _validate_fields(order_id: str, email: str, phone: str, name: str) -> str | None:
    order_id = order_id.strip()
    email = email.strip()
    phone = phone.strip()
    name = name.strip()
    if not order_id:
        return "An order number is required to search."
    if not order_id.isdigit():
        return "Order ID must contain numbers only."
    if not any((email, phone, name)):
        return "Add at least one more detail: email, phone number, or customer name."
    if email and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        return "Enter a valid email address."
    if phone and len(normalize_phone(phone)) < 10:
        return "Enter at least 10 phone digits."
    if name and len(name) < 2:
        return "Enter at least two characters of the customer's name."
    return None


def _run_lookup(order_id: str, email: str, phone: str, name: str) -> OrderLookup:
    return lookup_order(OrderCriteria(order_id, email, phone, name))


def _on_recent_chat_change() -> None:
    """Radio callback: queue a resume only on genuine user selection."""
    choice = st.session_state.get("recent_chat_choice")
    target_id = st.session_state.get("recent_chat_options_map", {}).get(choice)
    if target_id:
        st.session_state.resume_conversation_id = target_id


def render_recent_chats(session_id: str) -> None:
    """List this session's recent conversations; picking one resumes it.

    Selection is event-driven (radio on_change) — never inferred by comparing
    displayed state against the current view on every rerun, so a deliberate
    fresh start can't be overridden by whatever the radio happens to show.
    """
    conversations = _cached_recent_chats(session_id)
    if not conversations:
        return

    st.divider()
    st.header("Recent conversations")

    options: dict[str, str] = {"-- select a chat --": ""}
    for convo in conversations:
        state_label = "ended" if convo.get("ended_at") else "active"
        label = f"{format_timestamp(convo.get('updated_at'))} - {state_label}"
        options[label] = convo["id"]
    st.session_state.recent_chat_options_map = options

    st.radio(
        "Resume a conversation",
        list(options.keys()),
        key="recent_chat_choice",
        label_visibility="collapsed",
        on_change=_on_recent_chat_change,
    )


def render_manual_lookup_results() -> None:
    """Results panel for manual lookups, rendered inside the sidebar."""
    if "manual_lookup_results" not in st.session_state:
        return

    results = st.session_state.manual_lookup_results
    label = st.session_state.get("manual_lookup_label", "Manual lookup")
    st.divider()
    if st.session_state.get("manual_lookup_error"):
        st.error(st.session_state.manual_lookup_error)
        return
    if not results:
        st.warning(f"No orders found for {label}. Try a different lookup value.")
        return

    title_col, action_col = st.columns([5, 1])
    with title_col:
        st.markdown('<h3 class="lookup-panel-title">Results</h3>', unsafe_allow_html=True)
        st.caption(f"{label} - {len(results)} matching order{'s' if len(results) != 1 else ''}")
    with action_col:
        if st.button("Clear", use_container_width=True, key="clear_main_lookup"):
            st.session_state.pop("manual_lookup_results", None)
            st.session_state.pop("manual_lookup_label", None)
            st.rerun()

    for order in results:
        customer = order.get("customers") or {}
        summary = order_summary(order)
        name_only = st.session_state.get("manual_lookup_name_only", False)
        with st.container(border=True):
            header_col, total_col = st.columns([3, 2])
            with header_col:
                st.markdown(f"**Order #{summary['order_number']}**")
                if not name_only:
                    st.caption(customer.get("full_name", "Customer unavailable"))
            if not name_only:
                with total_col:
                    amount = float(summary["total_amount"])
                    st.metric("Total", f"{summary['currency']} {amount:,.2f}")
            st.metric("Status", summary["status"])

            delivery = summary["delivered_at"] or summary["estimated_delivery_at"]
            if delivery:
                delivery_label = "Delivered" if summary["delivered_at"] else "Estimated delivery"
                st.caption(f"{delivery_label}: {format_timestamp(delivery)}")
            if name_only:
                st.caption("Name-only verification: status and delivery timing only.")
            else:
                with st.expander("View complete order details"):
                    st.markdown(format_order_for_display(order))
                    timeline = get_order_timeline(order)
                    if timeline:
                        st.markdown("**Event timeline**")
                        for event in timeline:
                            st.markdown(f"- {format_event_line(event)}")


def render_sidebar(session_id: str) -> None:
    with st.sidebar:
        st.header("Find an order")
        st.caption("Verify with an order number and one matching detail.")

        with st.form("manual_lookup_form", clear_on_submit=False):
            order_id = f"{st.number_input('Order number', min_value=MIN_ORDER_ID, max_value=MAX_ORDER_ID, value=MIN_ORDER_ID, step=1, format='%04d', key='lookup_order_number'):04d}"
            st.caption("Add at least one more detail to verify the order before searching.")
            email = st.text_input("Email address (optional)", placeholder="you@example.com", key="lookup_email")
            phone = st.text_input("Phone number (optional)", placeholder="10-digit number", key="lookup_phone")
            name = st.text_input("Customer name (optional)", placeholder="Full or partial name", key="lookup_name")
            submitted = st.form_submit_button("Find order", use_container_width=True, type="primary")

        if submitted:
            validation_error = _validate_fields(order_id, email, phone, name)
            if validation_error:
                st.session_state.pop("manual_lookup_results", None)
                st.session_state.pop("manual_lookup_error", None)
                st.error(validation_error)
            else:
                secondary = next((value for value in (email, phone, name) if value.strip()), "")
                with st.spinner("Searching orders..."):
                    result = _run_lookup(order_id, email, phone, name)
                if result.status == "unavailable":
                    st.session_state.manual_lookup_results = []
                    st.session_state.manual_lookup_error = "Order lookup is temporarily unavailable. Please try again."
                    st.session_state.manual_lookup_name_only = False
                else:
                    st.session_state.pop("manual_lookup_error", None)
                    st.session_state.manual_lookup_results = result.orders if result.status == "found" else []
                    st.session_state.manual_lookup_name_only = result.name_only
                st.session_state.manual_lookup_label = f"Order {order_id} + {secondary.strip()}"

        render_manual_lookup_results()
        render_recent_chats(session_id)

        st.caption("Tip: use the chat for follow-up questions after an order is found.")
