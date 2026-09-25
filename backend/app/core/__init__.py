"""Cross-cutting infrastructure: logging today, more later (auth, errors)."""

from app.core.logging import configure_logging, get_logger

__all__ = ["configure_logging", "get_logger"]
