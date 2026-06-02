from .alpaca_client import AlpacaClient
from .base import BrokerClient, BrokerNotConfigured
from .etrade_client import ETradeClient


def get_broker(project) -> BrokerClient:
    """Factory — returns the right client for a project based on its
    broker_type column. Use this instead of instantiating AlpacaClient
    directly so the agents stay broker-agnostic.

    Raises BrokerNotConfigured if the project is missing credentials.
    """
    bt = getattr(project, "broker_type", "alpaca") or "alpaca"
    if bt == "alpaca":
        return AlpacaClient(project)
    if bt == "etrade":
        return ETradeClient(project)
    raise BrokerNotConfigured(f"Unknown broker_type: {bt!r}")


__all__ = ["AlpacaClient", "ETradeClient", "BrokerClient",
           "BrokerNotConfigured", "get_broker"]
