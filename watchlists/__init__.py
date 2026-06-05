"""Dynamic watchlist generation.

Replaces the static tier-baseline lists in intelligence.optimizer with
a market-aware refresher that runs once per trading day at market
open (and on demand via Optimize Now). Combines:

  * Stable CORE — tier-appropriate names that always belong (anchor
    set so the refresher never wipes out a working setup if today's
    movers are noisy).
  * MOMENTUM — today's top % gainers / losers in the broker's
    optionable universe, scored by abs(pct_change) × log(volume).
  * IV-RICH — names with high realized vol where premium is
    actually worth collecting (uses analytics.iv_rank when data
    is available).

Filters: BP fit, earnings within N days, illiquid chains.
"""
from .dynamic import refresh_watchlist, get_proposed_watchlist

__all__ = ["refresh_watchlist", "get_proposed_watchlist"]
