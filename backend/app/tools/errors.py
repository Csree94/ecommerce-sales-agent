"""Tool-layer error hierarchy (architecture audit §E: structured failures).

Agent tools translate integration errors into these types so a future
LangGraph graph can branch on the stable ``code`` without parsing messages.
Original client exceptions are chained as ``__cause__`` for logs; neither the
messages nor the causes ever contain credentials.
"""

from __future__ import annotations


class ToolError(Exception):
    """Base class for agent-tool failures.

    Carries a stable machine-readable ``code`` so graph nodes can degrade
    gracefully (e.g. "catalog unavailable") instead of crashing the turn.
    """

    code: str = "tool_error"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code


class ToolInputError(ToolError):
    """The tool input was rejected (invalid or unsupported parameters)."""

    code = "invalid_input"


class ToolNotFoundError(ToolError):
    """The requested resource does not exist upstream."""

    code = "not_found"


class ToolUnavailableError(ToolError):
    """Inventra could not be reached or did not answer reliably.

    Covers auth/config problems, timeouts, connection failures, upstream 5xx,
    rate limits and response-contract drift — failures the graph should
    surface as "I can't reach the catalog right now", never as empty results.
    """

    code = "unavailable"
