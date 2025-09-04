import os
from dotenv import load_dotenv
from supabase import create_client, Client
<<<<<<< HEAD
from langchain_google_genai import ChatGoogleGenerativeAI
=======
from langchain_openai import ChatOpenAI
import streamlit as st
>>>>>>> 6df1b21dcd9167a6badfdab7732af999e1a22f26

load_dotenv()

# Setup

<<<<<<< HEAD
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

=======
OPENAI_API_KEY = st.secrets['OPENAI_API_KEY']
SUPABASE_URL = st.secrets['SUPABASE_URL']
SUPABASE_KEY = st.secrets['SUPABASE_KEY']
>>>>>>> 6df1b21dcd9167a6badfdab7732af999e1a22f26
# System prompt for the assistant
SYSTEM_PROMPT = """You are a professional Order Status Assistant. You help customers check their order status using the database information provided.

When responding:
- When customers greet you, thank you, or make casual conversation, respond naturally and helpfully.
- Always be friendly and professional.
- Use the exact information from the DATABASE RESULTS section
- Never make up information not in the database
- For order changes, direct customers to human support
- Explain order status meanings when relevant

Order Status Meanings:
- Processing: Order received, being processed
- In Transit: Order dispatched, in transit  
- Out for Delivery: Order with delivery driver
- Delivered: Order successfully delivered
- Cancelled: Order cancelled
- Failed Delivery: Delivery attempt unsuccessful"""

def chatmodel():
    
    return ChatGoogleGenerativeAI(
        model="gemini-1.5-flash",        # Use a valid Gemini model name
        google_api_key=GEMINI_API_KEY,   # The correct parameter name
        temperature=0.2,
        convert_system_message_to_human=True # Often helpful for compatibility
    )
def supabase_client():
    
    return create_client(SUPABASE_URL, SUPABASE_KEY)
