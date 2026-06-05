"""Broker-derived brokerage fee sync.

After a contract closes (closed_contracts row appears with brokerage_fee
IS NULL), this package's sync job polls the broker for the actual
fee/commission charged on each fill and updates the row. Alpaca
exposes this via /v2/account/activities; ETrade via
/v1/accounts/{accountIdKey}/transactions.

The runner schedules `sync_fees_for_project` every 15 minutes during
trading hours. UI distinguishes NULL (not yet synced — show '—')
from a real zero fee (commission-free broker — show '$0.00').
"""
from .sync import sync_fees_for_project

__all__ = ["sync_fees_for_project"]
