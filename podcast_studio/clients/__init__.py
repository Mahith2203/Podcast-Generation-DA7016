"""Clients for the outside services (OpenRouter language models, Bodhan voices and translation) and their offline stand-ins for demo mode and tests."""

from __future__ import annotations

from ..config import Settings
from .base import LLMError
from .bodhan import BodhanClient, BodhanError
from .mock import MockBodhan, MockLLM
from .openrouter import OpenRouterClient
from .usage import UsageMeter

__all__ = ["make_clients", "LLMError", "BodhanError", "UsageMeter", "OpenRouterClient", "BodhanClient",
           "MockLLM", "MockBodhan"]


def make_clients(settings: Settings, meter: UsageMeter | None = None):
    """Return (llm, bodhan, meter). Each job gets its own meter so costs are attributed to the right project."""
    meter = meter or UsageMeter()
    if settings.mock:
        return MockLLM(meter), MockBodhan(settings, meter), meter
    return OpenRouterClient(settings, meter), BodhanClient(settings, meter), meter
