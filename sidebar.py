"""Sidebar controls for fast, validated two-field manual order lookup."""

import re

import streamlit as st

from database import find_orders


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
    if phone and len(re.sub(r"\D", "", phone)) < 10:
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
    return find_orders(criteria, require_order_id=True)


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