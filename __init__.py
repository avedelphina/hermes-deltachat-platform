"""Delta Chat platform plugin for Hermes Agent."""

from pathlib import Path

# Import adapter registration
from .adapter import register_platform, register_rpc_tools


def register(ctx):
    """Register Delta Chat platform adapter and bundled skills."""
    register_platform(ctx)
    register_rpc_tools(ctx)
