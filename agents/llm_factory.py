"""Shared LLM factory.

Reads `llm_provider`, the per-provider key and model from `app_settings`,
and returns a configured LangChain chat model — or None if no key is set.
Used by the Strategist agent and the chat assistant.
"""
from __future__ import annotations

import logging

from langchain_core.language_models.chat_models import BaseChatModel

from db.settings_store import AppSettings

logger = logging.getLogger(__name__)


def build_llm(*, temperature: float | None = None,
              max_tokens: int | None = None,
              purpose: str = "strategist") -> BaseChatModel | None:
    """Build the configured chat model.

    `purpose` ∈ {"strategist", "chat"} — chat uses a separately configurable
    (usually more capable) model so the strategist can keep a cheap one.
    """
    provider = (AppSettings.get("llm_provider", "anthropic") or "anthropic").lower()
    t = temperature if temperature is not None else AppSettings.get("anthropic_temperature", 0.2)
    m = max_tokens if max_tokens is not None else AppSettings.get("anthropic_max_tokens", 2048)

    if provider == "google":
        key = AppSettings.get("google_api_key")
        if not key:
            return None
        try:
            from langchain_google_genai import ChatGoogleGenerativeAI
        except ImportError:
            logger.error("langchain-google-genai not installed")
            return None
        if purpose == "chat":
            model = AppSettings.get("google_chat_model", "gemini-2.5-flash")
        else:
            model = AppSettings.get("google_model", "gemini-2.5-flash")
        return ChatGoogleGenerativeAI(
            google_api_key=key,
            model=model,
            temperature=t,
            max_output_tokens=m,
        )

    key = AppSettings.get("anthropic_api_key")
    if not key:
        return None
    try:
        from langchain_anthropic import ChatAnthropic
    except ImportError:
        logger.error("langchain-anthropic not installed")
        return None
    if purpose == "chat":
        model = AppSettings.get("anthropic_chat_model",
                                AppSettings.get("anthropic_model", "claude-sonnet-4-6"))
    else:
        model = AppSettings.get("anthropic_model", "claude-sonnet-4-6")
    return ChatAnthropic(
        api_key=key,
        model=model,
        temperature=t,
        max_tokens=m,
    )


def provider_label(purpose: str = "strategist") -> str:
    """Short human-readable id of the currently configured provider/model."""
    provider = (AppSettings.get("llm_provider", "anthropic") or "anthropic").lower()
    if provider == "google":
        if purpose == "chat":
            model = AppSettings.get("google_chat_model", "gemini-2.5-flash")
        else:
            model = AppSettings.get("google_model", "gemini-2.5-flash")
        return f"google/{model}"
    if purpose == "chat":
        model = AppSettings.get("anthropic_chat_model",
                                AppSettings.get("anthropic_model", "claude-sonnet-4-6"))
    else:
        model = AppSettings.get("anthropic_model", "claude-sonnet-4-6")
    return f"anthropic/{model}"
