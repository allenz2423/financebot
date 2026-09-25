"""Platform-neutral agent runtime contracts.

The Discord bot remains the current entry point, but the core turn contracts
live here so delivery, provider, persistence, and finance policy can evolve
independently.
"""

from .context import ContextBlock, ContextBundle
from .events import AgentEvent, MessageEvent, TurnRequest
from .runtime import AgentRuntime, RuntimeHooks

__all__ = [
    "AgentEvent",
    "AgentRuntime",
    "ContextBlock",
    "ContextBundle",
    "MessageEvent",
    "RuntimeHooks",
    "TurnRequest",
]
