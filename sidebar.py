"""Sidebar controls for fast, validated two-field manual order lookup."""

import re

import streamlit as st

from database import find_orders, format_timestamp, list_conversations, normalize_phone


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
    conversations = list_conversations()
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

        if "manual_lookup_results" in st.session_state:
            st.divider()
            st.caption(st.session_state.get("manual_lookup_label", "Latest search"))
            result_count = len(st.session_state.manual_lookup_results)
            if result_count:
                st.success(f"{result_count} result{'s' if result_count != 1 else ''} shown in the workspace.")
            else:
                st.info("No matching orders found. Check the value and try again.")
            if st.button("Clear lookup", use_container_width=True):
                st.session_state.pop("manual_lookup_results", None)
                st.session_state.pop("manual_lookup_label", None)
                st.rerun()

        st.divider()
        st.caption("Tip: use the chat for follow-up questions after an order is found.")

        render_recent_chats()