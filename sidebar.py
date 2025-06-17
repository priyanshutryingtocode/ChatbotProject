import streamlit as st
from database import (
    get_order_by_id,
    get_orders_by_email,
    get_orders_by_phone,
    format_order_for_display
)

def render_sidebar():
    
    with st.sidebar:
        st.header("🔍 Quick Lookup")
        
        tab1, tab2, tab3 = st.tabs(["Order ID", "Email", "Phone"])

        with tab1:
            render_order_id_search()

        with tab2:
            render_email_search()

        with tab3:
            render_phone_search()

def render_order_id_search():

    order_id = st.number_input("Order ID:", min_value=1000, step=1, key="sidebar_order")
    if st.button("Search", key="search_order"):
        if order_id:
            result = get_order_by_id(order_id)
            if result:
                st.markdown(format_order_for_display(result))
            else:
                st.error("Order not found")

def render_email_search():

    email = st.text_input("Email:", key="sidebar_email")
    if st.button("Search", key="search_email"):
        if email:
            results = get_orders_by_email(email)
            if results:
                st.success(f"Found {len(results)} order(s)")
                for order in results:
                    st.markdown(format_order_for_display(order))
                    st.divider()
            else:
                st.error("No orders found")

def render_phone_search():

    phone = st.text_input("Phone:", key="sidebar_phone") 
    if st.button("Search", key="search_phone"):
        if phone:
            results = get_orders_by_phone(phone)
            if results:
                st.success(f"Found {len(results)} order(s)")
                for order in results:
                    st.markdown(format_order_for_display(order))
                    st.divider()
            else:
                st.error("No orders found")