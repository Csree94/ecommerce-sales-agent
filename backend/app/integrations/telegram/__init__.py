"""Telegram integration (webhook + Bot API client).

The ONLY path between Telegram and the sales agent. Structure mirrors the
Inventra/LLM integrations (audit §H):

- ``client``  — ``TelegramClient`` (httpx Bot API; the only outbound path)
- ``schemas`` — webhook update contracts + defensive normalization
- ``errors``  — typed integration errors for safe failure handling

The bot token lives only inside the client's URL path; webhook verification
uses ``TELEGRAM_WEBHOOK_SECRET`` via constant-time comparison. Neither secret
ever reaches logs, state, prompts, or persistence.
"""

from app.integrations.telegram.client import TelegramClient
from app.integrations.telegram.errors import (
    TelegramAuthError,
    TelegramConfigError,
    TelegramConnectionError,
    TelegramError,
    TelegramRateLimitedError,
    TelegramResponseError,
    TelegramServerError,
    TelegramTimeoutError,
)
from app.integrations.telegram.schemas import (
    InboundMessage,
    TelegramBotInfo,
    TelegramChat,
    TelegramMessage,
    TelegramUpdate,
    TelegramUser,
    extract_inbound,
    parse_update,
)

__all__ = [
    "InboundMessage",
    "TelegramAuthError",
    "TelegramBotInfo",
    "TelegramChat",
    "TelegramClient",
    "TelegramConfigError",
    "TelegramConnectionError",
    "TelegramError",
    "TelegramMessage",
    "TelegramRateLimitedError",
    "TelegramResponseError",
    "TelegramServerError",
    "TelegramTimeoutError",
    "TelegramUpdate",
    "TelegramUser",
    "extract_inbound",
    "parse_update",
]
