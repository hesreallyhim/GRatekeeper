from __future__ import annotations

import logging
from importlib import import_module
from typing import Callable, cast

_RichHandler: Callable[..., logging.Handler] | None = None
_escape: Callable[[str], str]

try:
    rich_logging = import_module("rich.logging")
    rich_markup = import_module("rich.markup")
except ImportError:  # pragma: no cover
    RICH_AVAILABLE = False

    def _noop_escape(value: str) -> str:
        return value

    _escape = _noop_escape
else:
    RICH_AVAILABLE = True
    _RichHandler = cast(Callable[..., logging.Handler], getattr(rich_logging, "RichHandler"))
    _escape = cast(Callable[[str], str], getattr(rich_markup, "escape"))


def ensure_rich_logging() -> None:
    """Attach a Rich handler to the gratekeeper logger if none is configured."""

    if not RICH_AVAILABLE or _RichHandler is None:
        return

    logger = logging.getLogger("gratekeeper")
    if logger.handlers:
        return

    handler = _RichHandler(markup=True, rich_tracebacks=True)
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False


def format_status(code: int, *, is_rate_limit: bool = False) -> str:
    """Return a colorized HTTP status code if Rich is available."""

    if not RICH_AVAILABLE:
        return str(code)

    if is_rate_limit:
        style = "bold red"
    elif code >= 500:
        style = "bold red"
    elif code >= 400:
        style = "bold yellow"
    else:
        style = "bold green"

    return f"[{style}]{code}[/{style}]"


def style_text(message: str, style: str, *, escape_message: bool = True) -> str:
    if not RICH_AVAILABLE:
        return message
    if escape_message:
        message = _escape(message)
    return f"[{style}]{message}[/{style}]"
