"""Backwards-compatible re-export.

The real BullPutSpreadStrategy lives in
:mod:`strategies.vertical_spreads`. This module used to host a parallel
stub that never emitted trades AND created a name collision with the
real implementation in vertical_spreads.py. The stub has been retired;
this re-export keeps any historical `from strategies.bull_put_spread
import BullPutSpreadStrategy` import sites working.
"""
from __future__ import annotations

from .vertical_spreads import BullPutSpreadStrategy

__all__ = ["BullPutSpreadStrategy"]
