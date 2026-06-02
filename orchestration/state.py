from typing import Any, TypedDict


class AgentPortfolioState(TypedDict, total=False):
    project_id: str
    target_tickers: list[str]
    candidate_details: list[dict[str, Any]]
    selected_trades: list[dict[str, Any]]
    risk_clearance: bool
    guardrail_actions: list[dict[str, Any]]
    execution_status: str   # SCANNING | ACTIVE_HOLD | TRADE_COMPLETED
    execution_results: list[dict[str, Any]]
    cycle_count: int
