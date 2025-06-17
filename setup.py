import os
from dotenv import load_dotenv
from supabase import create_client, Client
from langchain_openai import ChatOpenAI

load_dotenv()

# Setup

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

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
    
    return ChatOpenAI(
        base_url="https://openrouter.ai/api/v1",
        api_key=OPENAI_API_KEY,
        model="meta-llama/llama-3.3-8b-instruct:free",
        temperature=0.2
    )

def supabase_client():
    
    return create_client(SUPABASE_URL, SUPABASE_KEY)