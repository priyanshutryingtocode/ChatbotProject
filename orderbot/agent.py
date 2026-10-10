"""LangChain agent construction for the read-only customer assistant."""

from langchain.agents import create_agent

from .config import SYSTEM_PROMPT, chatmodel
from .tools import get_order_status, search_policies


def build_agent(order_tool=get_order_status, action_tools=()):
    """Build an agent; handler-bound tools keep per-session controls isolated."""
    return create_agent(
        model=chatmodel(),
        tools=[order_tool, search_policies, *action_tools],
        system_prompt=SYSTEM_PROMPT,
    )
