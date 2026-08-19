"""Sidebar controls for fast, validated manual order lookup."""

import re

import streamlit as st

from database import (
    get_order_by_id,
    get_orders_by_email,
    get_orders_by_phone,
    search_orders_by_name,
)

LOOKUP_OPTIONS = {
    "Order ID": "Search a single order number, e.g. 900000",
    "Email address": "Search all orders for an exact email address",
    "Phone number": "Use a 10-digit phone number; formatting is accepted",
    "Customer name": "Search a full or partial customer name",
}


def _validate_lookup(lookup_type: str, value: str) -> str | None:
    value = value.strip()
    if not value:
        return "Enter a value to search."
    if lookup_type == "Order ID" and not value.isdigit():
        return "Order ID must contain numbers only."
    if lookup_type == "Email address" and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value):
        return "Enter a valid email address."
    if lookup_type == "Phone number" and len(re.sub(r"\D", "", value)) < 10:
        return "Enter at least 10 phone digits."
    if lookup_type == "Customer name" and len(value) < 2:
        return "Enter at least two characters of the customer's name."
    return None


def _run_lookup(lookup_type: str, value: str) -> list[dict]:
    if lookup_type == "Order ID":
        order = get_order_by_id(value)
        return [order] if order else []
    if lookup_type == "Email address":
        return get_orders_by_email(value)
    if lookup_type == "Phone number":
        return get_orders_by_phone(value)
    return search_orders_by_name(value)


def render_sidebar() -> None:
    with st.sidebar:
        st.header("Manual lookup")
        st.caption("Run a direct database search without using the assistant.")

        lookup_type = st.selectbox("Search by", options=list(LOOKUP_OPTIONS), key="lookup_type")
        st.caption(LOOKUP_OPTIONS[lookup_type])
        with st.form("manual_lookup_form", clear_on_submit=False):
            if lookup_type == "Order ID":
                lookup_value = f"{st.number_input('Order number', min_value=1, max_value=1000, value=1, step=1, format='%04d', key='lookup_order_number'):04d}"
            else:
                lookup_value = st.text_input(
                    "Search value",
                    placeholder="Enter the value to search",
                    key="lookup_value",
                    help="Use the same customer information stored in the order system.",
                )
            submitted = st.form_submit_button("Search orders", use_container_width=True, type="primary")

        if submitted:
            validation_error = _validate_lookup(lookup_type, lookup_value)
            if validation_error:
                st.session_state.pop("manual_lookup_results", None)
                st.error(validation_error)
            else:
                with st.spinner("Searching orders..."):
                    st.session_state.manual_lookup_results = _run_lookup(lookup_type, lookup_value.strip())
                st.session_state.manual_lookup_label = f"{lookup_type}: {lookup_value.strip()}"

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
