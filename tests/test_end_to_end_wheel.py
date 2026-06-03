"""End-to-end pipeline test.

Asserts that one Worker cycle on a clean, known-good project produces:

    Scanner.SCAN  ->  Strategist.DECIDE (with >=1 approved trade)
        ->  Guardrail.RISK (final, approved_trades > 0)
        ->  Executor.EXECUTE (status SUBMITTED)
        ->  Worker.LOOP (status TRADE_COMPLETED, trades > 0)
    +  the FakeAlpacaClient sees at least one submit_limit_option call

These five assertions cover ~80% of the failure modes we hit in
production this week (BP mismatch, OUTPUT INSERTED bugs, concentration
math, SQL-Server-ism, runner not starting). If this test had been wired
to the deploy pipeline, every one of those bugs would have been blocked
before reaching production.
"""
from __future__ import annotations

import asyncio

import pytest


@pytest.mark.asyncio
async def test_one_cycle_produces_a_trade(sample_project, patched_alpaca):
    """The headline test. If this passes, the wheel can trade."""
    from db.repositories import EventsRepo
    from workers.tenant_worker import TenantWorker

    worker = TenantWorker(sample_project.project_id)
    await worker._run_one_cycle()

    events = EventsRepo.recent(sample_project.project_id, limit=80)
    nodes_seen = {(e["node_name"], e["event_type"]) for e in events}

    # 1. Every agent fired
    assert ("Scanner", "SCAN") in nodes_seen, \
        "Scanner did not run — check market clock / mode gate / extended hours"
    assert ("Strategist", "DECIDE") in nodes_seen, \
        "Strategist did not produce a DECIDE event — Scanner returned 0 candidates"
    # Guardrail logs multiple RISK events (per-trade + final summary)
    guardrail_finals = [
        e for e in events
        if e["node_name"] == "Guardrail" and e["event_type"] == "RISK"
        and "approved_trades" in (e.get("payload") or {})
    ]
    assert guardrail_finals, \
        "Guardrail did not produce a final RISK summary event"
    assert ("Executor", "EXECUTE") in nodes_seen, \
        "Executor was skipped (Guardrail approved zero trades)"

    # 2. Guardrail produced >= 1 approved trade
    g = guardrail_finals[0]
    approved = (g.get("payload") or {}).get("approved_trades") or []
    assert len(approved) >= 1, \
        f"Guardrail approved 0 trades. Reasons in events: {events[:5]}"

    # 3. Executor submitted at least one order
    execs = [e for e in events
             if e["node_name"] == "Executor" and e["event_type"] == "EXECUTE"]
    assert execs
    statuses = []
    for ex in execs:
        for r in (ex.get("payload") or {}).get("results") or []:
            statuses.append((r.get("status") or "?").upper())
    assert any(s == "SUBMITTED" for s in statuses), \
        f"No SUBMITTED order in Executor results. Got: {statuses}"

    # 4. The fake broker actually saw a submit_limit_option call
    client = patched_alpaca["client"]
    assert client is not None, "AlpacaClient was never instantiated"
    orders = client.submitted_orders()
    assert len(orders) >= 1, \
        "FakeAlpacaClient never saw an order. Pipeline ran but never " \
        "actually called submit_limit_option."

    # 5. Worker.LOOP marks the cycle complete
    worker_loops = [
        e for e in events
        if e["node_name"] == "Worker" and e["event_type"] == "LOOP"
    ]
    assert worker_loops, "Worker.LOOP not logged"
    payload = worker_loops[0].get("payload") or {}
    assert payload.get("trades", 0) >= 1, \
        f"Worker.LOOP says trades=0 but Executor saw {len(orders)} orders. " \
        f"State propagation bug in the LangGraph wiring."


@pytest.mark.asyncio
async def test_mode_paused_skips_wheel(sample_project, patched_alpaca):
    """If strategy_mode=paused, the cycle should skip cleanly without
    burning any Alpaca API calls — no Scanner / Strategist / Executor
    events, no orders submitted."""
    from db.repositories import EventsRepo
    from db.settings_store import ProjectSettings
    from workers.tenant_worker import TenantWorker

    ProjectSettings.set(sample_project.project_id, "strategy_mode",
                        "paused", value_type="string")

    worker = TenantWorker(sample_project.project_id)
    await worker._run_one_cycle()

    events = EventsRepo.recent(sample_project.project_id, limit=20)
    # The only event from this cycle should be a Worker.LOOP marked
    # skipped, plus the SETTING_CHANGE we just made.
    nodes = {e["node_name"] for e in events}
    assert "Scanner" not in nodes, "Scanner ran despite paused mode"
    assert "Strategist" not in nodes, "Strategist ran despite paused mode"
    assert "Executor" not in nodes, "Executor ran despite paused mode"

    client = patched_alpaca["client"]
    if client is not None:
        assert client.submitted_orders() == [], \
            "Orders submitted even though mode=paused"


@pytest.mark.asyncio
async def test_dry_run_does_not_submit_orders(sample_project, patched_alpaca):
    """dry_run=true must let the strategist + guardrail run normally but
    suppress actual order submission to the broker."""
    from db.repositories import EventsRepo
    from db.settings_store import ProjectSettings
    from workers.tenant_worker import TenantWorker

    ProjectSettings.set(sample_project.project_id, "dry_run", True,
                        value_type="bool")

    worker = TenantWorker(sample_project.project_id)
    await worker._run_one_cycle()

    events = EventsRepo.recent(sample_project.project_id, limit=40)
    execs = [e for e in events
             if e["node_name"] == "Executor" and e["event_type"] == "EXECUTE"]
    assert execs, "Executor should still run in dry_run mode"
    statuses = []
    for ex in execs:
        for r in (ex.get("payload") or {}).get("results") or []:
            statuses.append((r.get("status") or "?").upper())
    assert "DRY_RUN" in statuses or "DRY-RUN" in statuses, \
        f"Expected at least one DRY_RUN status, got {statuses}"
    assert "SUBMITTED" not in statuses, \
        "dry_run is True but an order was actually submitted"

    client = patched_alpaca["client"]
    if client is not None:
        assert client.submitted_orders() == [], \
            "FakeAlpacaClient saw orders despite dry_run=True"
