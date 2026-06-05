"""LangGraph topology with recursive loop.

  Scanner -> Strategist -> Guardrail --(cleared)--> Executor -> (loop back)
                                    \--(blocked)--> Scanner

The closed loop is implemented at the worker level by re-invoking the compiled
graph after each cycle. We keep the in-graph edges acyclic to avoid LangGraph's
default recursion-limit pitfalls.
"""
from __future__ import annotations

from langgraph.graph import END, StateGraph

from agents import (
    execute_orders_node,
    risk_guardrail_node,
    scan_movers_node,
)
from agents.strategy_dispatcher import strategy_dispatcher_node

from .state import AgentPortfolioState


def _route_after_guardrail(state: AgentPortfolioState) -> str:
    if not state.get("risk_clearance"):
        return "END"
    if not state.get("selected_trades"):
        return "END"
    return "Executor"


def build_graph():
    workflow = StateGraph(AgentPortfolioState)
    workflow.add_node("Scanner",    scan_movers_node)
    # Strategist routes on the project's strategy_mode setting — wheel
    # (default) falls back to analyze_wheel_node; spreads + intraday
    # branch to their own nodes via the dispatcher.
    workflow.add_node("Strategist", strategy_dispatcher_node)
    workflow.add_node("Guardrail",  risk_guardrail_node)
    workflow.add_node("Executor",   execute_orders_node)

    workflow.set_entry_point("Scanner")
    workflow.add_edge("Scanner", "Strategist")
    workflow.add_edge("Strategist", "Guardrail")
    workflow.add_conditional_edges(
        "Guardrail",
        _route_after_guardrail,
        {"Executor": "Executor", "END": END},
    )
    workflow.add_edge("Executor", END)

    return workflow.compile()
