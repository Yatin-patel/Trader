"""AI chat endpoint — uses whichever LLM is configured globally.

The assistant gets a system prompt explaining it's embedded in the autonomous
trader, plus a compact snapshot of the current project state so it can answer
questions like "why did the strategist reject NVDA last cycle?" without the
user having to paste anything.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, HTTPException
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from pydantic import BaseModel

from langchain_core.messages import ToolMessage

from agents.llm_factory import build_llm, provider_label
from db.repositories import EventsRepo, PositionsRepo, ProjectsRepo, WheelRepo
from db.settings_store import AppSettings, ProjectSettings

from .chat_tools import build_tools

logger = logging.getLogger(__name__)
router = APIRouter()


class ChatMessage(BaseModel):
    role: str   # "user" | "assistant"
    content: str


class ChatIn(BaseModel):
    message: str
    history: list[ChatMessage] = []
    project_id: str | None = None


def _build_system_prompt(project_id: str | None) -> str:
    """Minimal system prompt. Keep it short so Gemini reaches for tools
    instead of trying to answer from the prompt itself."""
    provider = provider_label("chat")
    base = (
        f"You are the assistant in an autonomous options-wheel trader "
        f"({provider}). Answer concisely. For ANY question about a stock, "
        f"the account, option chains, positions, the market clock, or recent "
        f"agent activity, you MUST first call the relevant tool. Never invent "
        f"prices, percentages, or greeks. After the tools return, summarize "
        f"in 2-4 sentences with the real numbers."
    )
    if not project_id:
        return base
    project = ProjectsRepo.get(project_id)
    if project is None:
        return base
    return base + f"\nActive project: {project.project_name} ({project_id})."


def _build_unused_for_compatibility(project_id: str | None):
    """Legacy code path retained for reference; not called."""
    project = ProjectsRepo.get(project_id) if project_id else None
    if project is None:
        return ""
    settings = {s.key: s.value for s in ProjectSettings.list_for_project(project_id)}
    positions = PositionsRepo.list_open(project_id)
    contracts = WheelRepo.list_open(project_id)
    recent = EventsRepo.recent(project_id, limit=15)
    ctx = [f"## Active project: {project.project_name} ({project_id})"]
    ctx.append(f"- Alpaca endpoint: {project.alpaca_base_url}")
    ctx.append(f"- Max allocation: ${project.max_equity_allocation:,.2f}")
    ctx.append(f"- Open stock positions: {len(positions)}")
    ctx.append(f"- Open option contracts: {len(contracts)}")
    ctx.append("")
    ctx.append("## Project settings (relevant ones)")
    keys = ["stop_loss_dollars", "csp_delta_min", "csp_delta_max"]
    for k in keys:
        if k in settings:
            ctx.append(f"- {k} = {settings[k]}")
    ctx.append("")
    ctx.append("## Last 15 agent events (newest first)")
    for e in recent:
        payload = e.get("payload") or {}
        summary = ""
        if isinstance(payload, dict):
            if e["node_name"] == "Scanner":
                summary = f"selected={payload.get('selected', [])[:5]}"
            elif e["node_name"] == "Strategist":
                sel = payload.get("selected") or []
                rej = payload.get("rejections") or []
                if sel:
                    summary = f"approved={[t.get('ticker') for t in sel]}"
                else:
                    summary = "rejected=" + "; ".join(
                        f"{r.get('ticker')}:{r.get('reason')[:60]}" for r in rej[:3]
                    )
            elif e["node_name"] == "Guardrail":
                summary = (f"bp=${payload.get('buying_power', 0):,.0f} "
                           f"approved={len(payload.get('approved_trades') or [])}")
            elif e["node_name"] == "Executor":
                results = payload.get("results") or []
                statuses: dict[str, int] = {}
                for r in results:
                    s = r.get("status", "?")
                    statuses[s] = statuses.get(s, 0) + 1
                summary = ",".join(f"{k}={v}" for k, v in statuses.items())
            elif e["node_name"] == "Worker":
                summary = (payload.get("skipped")
                           or f"cycle={payload.get('cycle')}")
        ctx.append(f"- [{e['created_at']}] {e['node_name']}.{e['event_type']}: {summary}")
    return "\n".join(ctx)


@router.post("/api/chat")
async def chat(payload: ChatIn) -> dict[str, Any]:
    llm = build_llm(purpose="chat", max_tokens=4096)
    if llm is None:
        provider = AppSettings.get("llm_provider", "anthropic")
        key_setting = "google_api_key" if provider == "google" else "anthropic_api_key"
        raise HTTPException(
            400,
            f"No LLM configured. Set {key_setting} in Global Settings.",
        )

    system_text = _build_system_prompt(payload.project_id)
    msgs: list[Any] = [SystemMessage(content=system_text)]
    for m in payload.history[-20:]:
        if m.role == "user":
            msgs.append(HumanMessage(content=m.content))
        else:
            msgs.append(AIMessage(content=m.content))
    # Append the user message with an inline tool-use nudge.
    msgs.append(HumanMessage(content=(
        payload.message
        + "\n\n(Use the available tools to fetch real-time data before answering.)"
    )))

    tools = build_tools(payload.project_id)
    tool_lookup = {t.name: t for t in tools}
    llm_with_tools = llm.bind_tools(tools)

    tools_used: list[str] = []
    final_content = ""

    def _extract_text(content: Any) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for c in content:
                if isinstance(c, str):
                    parts.append(c)
                elif isinstance(c, dict):
                    if c.get("type") == "text" and "text" in c:
                        parts.append(c["text"])
                else:
                    txt = getattr(c, "text", None)
                    if txt:
                        parts.append(txt)
            return "".join(parts)
        return str(content) if content else ""

    try:
        for iteration in range(10):              # max tool-loop iterations
            resp = await asyncio.to_thread(llm_with_tools.invoke, msgs)
            msgs.append(resp)
            tool_calls = getattr(resp, "tool_calls", None) or []
            text = _extract_text(resp.content)
            if not tool_calls:
                final_content = text
                break
            # If model also wrote text alongside its tool calls, keep it as a
            # fallback in case it never produces a final tool-free turn.
            if text and not final_content:
                final_content = text
            for call in tool_calls:
                name = call.get("name")
                args = call.get("args") or {}
                tools_used.append(name)
                tool = tool_lookup.get(name)
                if tool is None:
                    result = f"unknown tool: {name}"
                else:
                    try:
                        result = await asyncio.to_thread(tool.invoke, args)
                    except Exception as e:
                        result = f"tool error: {e}"
                msgs.append(ToolMessage(
                    content=str(result),
                    name=str(name) if name else "tool",
                    tool_call_id=str(call.get("id") or name or "call"),
                ))
        # If model never produced final text, do a forced synthesis turn.
        # We build a clean message list from scratch — Gemini chokes on the
        # raw tool-call/tool-response structure when invoked without
        # `bind_tools`. We collapse the tool results to plain text instead.
        if not final_content:
            tool_log: list[str] = []
            for m in msgs:
                if isinstance(m, ToolMessage):
                    tool_log.append(
                        f"[tool {m.name}] {str(m.content)[:600]}"
                    )
            tool_dump = "\n".join(tool_log) if tool_log else "(no tool results)"
            clean_msgs = [
                SystemMessage(content=system_text),
                HumanMessage(content=payload.message),
                HumanMessage(content=(
                    "Here are the tool results I gathered:\n"
                    f"{tool_dump}\n\n"
                    "Answer the user in 2-4 sentences using only the data above. "
                    "Do not call any tools."
                )),
            ]
            try:
                final_resp = await asyncio.to_thread(llm.invoke, clean_msgs)
                final_content = _extract_text(final_resp.content)
            except Exception as e:
                logger.exception("forced synthesis failed")
                final_content = (f"I retrieved data but the model couldn't compose "
                                 f"a response. ({e})")
        return {
            "response": final_content,
            "provider": provider_label("chat"),
            "tools_used": tools_used,
        }
    except Exception as e:
        logger.exception("chat invoke failed")
        raise HTTPException(500, f"LLM error: {e}")
