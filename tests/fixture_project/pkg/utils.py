"""Utility functions."""

# Circular import with models (for testing CIRCULAR_IMPORT rule)
from pkg.models import User


def helper_function(name: str) -> str:
    """Helper that processes a name."""
    return f"Hello, {name}!"


def unused_function(x: int) -> int:
    """This is imported but never called — tests DEAD_SYMBOL detection."""
    return x * 2


async def async_helper(data: list) -> list:
    """An async function for testing async detection."""
    return sorted(data)
