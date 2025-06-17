import streamlit as st
from chat_handler import OrderChatHandler
from sidebar import render_sidebar

st.set_page_config(
    page_title="Order Status Assistant",
    page_icon="🛍️",
    layout="wide"
)

def initialize_session_state():
    
    if "messages" not in st.session_state:
        st.session_state.messages = []
    
    if "last_db_results" not in st.session_state:
        st.session_state.last_db_results = {}
    
    if "chat_handler" not in st.session_state:
        st.session_state.chat_handler = OrderChatHandler()

def render_chat_interface():

    st.title("🛍️ Order Status Assistant")

    col1, col2 = st.columns([1, 3])
    with col1:
        if st.button("🔄 New Chat"):
            st.session_state.messages = []
            st.session_state.last_db_results = {}
            st.rerun()

    for message in st.session_state.messages:
        with st.chat_message(message["role"]):
            st.markdown(message["content"])


    prompt = st.chat_input("Enter your order ID, email, phone, or name...")
    if prompt:
        handle_user_input(prompt)
        st.rerun()  

def handle_user_input(user_input):

    st.session_state.messages.append({"role": "user", "content": user_input})

    try:
        with st.spinner("..."):
            ai_response, db_results = st.session_state.chat_handler.process_user_message(user_input)

        if db_results:
            st.session_state.last_db_results = db_results

        st.session_state.messages.append({"role": "assistant", "content": ai_response})

    except Exception as e:
        error_msg = f"I apologize, but I encountered an error: {str(e)}"
        st.session_state.messages.append({"role": "assistant", "content": error_msg})

def render_footer():

    st.markdown("---")
    st.markdown("🔧 *For order modifications, please contact our support team*")

def main():


    initialize_session_state()
    col1, col2 = st.columns([3, 1])

    with col1:
        render_chat_interface()
        render_footer()

    with col2:
        render_sidebar()

if __name__ == "__main__":
    main()
