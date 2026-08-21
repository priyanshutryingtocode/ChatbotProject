"""Sidebar controls: manual two-field order lookup, results panel, recent chats."""

import re

import streamlit as st

from database import (
    find_orders,
    format_event_line,
    format_order_for_display,
    format_order_number,
    format_timestamp,
    list_conversations,
    normalize_phone,
)


@st.cache_data(ttl=20, show_spinner=False)
def _cached_recent_chats(limit: int = 10):
    """Brief cache so the sidebar stops querying Supabase on every rerun."""
    return list_conversations(limit)


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


def _run_lookup(order_id: str, email: str, phone: str, name: str) -> list[dict]:
    criteria = {"order_id": order_id}
    if email.strip():
        criteria["email"] = email.strip()
    if phone.strip():
        criteria["phone"] = phone.strip()
    if name.strip():
        criteria["name"] = name.strip()
    try:
        return find_orders(criteria, require_order_id=True)
    except ValueError:
        return []


def _on_recent_chat_change() -> None:
    """Radio callback: queue a resume only on genuine user selection."""
    choice = st.session_state.get("recent_chat_choice")
    target_id = st.session_state.get("recent_chat_options_map", {}).get(choice)
    if target_id:
        st.session_state.resume_conversation_id = target_id


def render_recent_chats() -> None:
    """List recent conversations; picking one resumes it in the workspace.

    Selection is event-driven (radio on_change) — never inferred by comparing
    displayed state against the current view on every rerun, so a deliberate
    fresh start can't be overridden by whatever the radio happens to show.
    """
    conversations = _cached_recent_chats()
    if not conversations:
        return

    st.divider()
    st.header("Recent chats")

    options: dict[str, str] = {"— select a chat —": ""}
    for convo in conversations:
        state_label = "ended" if convo.get("ended_at") else "active"
        label = f"{format_timestamp(convo.get('created_at'))} · {state_label}"
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
    if not results:
        st.warning(f"No orders found for {label}. Try a different lookup value.")
        return

    title_col, action_col = st.columns([5, 1])
    with title_col:
        st.markdown('<h3 class="lookup-panel-title">Results</h3>', unsafe_allow_html=True)
        st.caption(f"{label} · {len(results)} matching order{'s' if len(results) != 1 else ''}")
    with action_col:
        if st.button("Clear", use_container_width=True, key="clear_main_lookup"):
            st.session_state.pop("manual_lookup_results", None)
            st.session_state.pop("manual_lookup_label", None)
            st.rerun()

    for order in results:
        shipment = (order.get("shipments") or [{}])[0]
        customer = order.get("customers") or {}
        with st.container(border=True):
            header_col, total_col = st.columns([3, 2])
            with header_col:
                st.markdown(f"**Order #{format_order_number(order.get('public_order_id'))}**")
                st.caption(customer.get("full_name", "Customer unavailable"))
            with total_col:
                amount = float(order.get("total_amount") or 0)
                st.metric("Total", f"{order.get('currency', 'USD')} {amount:,.2f}")
            st.metric("Status", order.get("status", "Unknown"))

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


def render_sidebar() -> None:
    with st.sidebar:
        st.header("Manual lookup")
        st.caption("Run a direct database search without using the assistant.")

        with st.form("manual_lookup_form", clear_on_submit=False):
            order_id = f"{st.number_input('Order number', min_value=1, max_value=1000, value=1, step=1, format='%04d', key='lookup_order_number'):04d}"
            st.caption("Add at least one more detail to verify the order before searching.")
            email = st.text_input("Email address (optional)", placeholder="you@example.com", key="lookup_email")
            phone = st.text_input("Phone number (optional)", placeholder="10-digit number", key="lookup_phone")
            name = st.text_input("Customer name (optional)", placeholder="Full or partial name", key="lookup_name")
            submitted = st.form_submit_button("Search orders", use_container_width=True, type="primary")

        if submitted:
            validation_error = _validate_fields(order_id, email, phone, name)
            if validation_error:
                st.session_state.pop("manual_lookup_results", None)
                st.error(validation_error)
            else:
                secondary = next((value for value in (email, phone, name) if value.strip()), "")
                with st.spinner("Searching orders..."):
                    st.session_state.manual_lookup_results = _run_lookup(order_id, email, phone, name)
                st.session_state.manual_lookup_label = f"Order {order_id} + {secondary.strip()}"

        render_manual_lookup_results()
        render_recent_chats()

        st.caption("Tip: use the chat for follow-up questions after an order is found.")