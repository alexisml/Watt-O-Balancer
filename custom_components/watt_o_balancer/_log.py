"""Centralized logging for the EV Charger Load Balancing integration.

All modules import their logger from here instead of calling
``logging.getLogger(__name__)`` directly.  This gives us a single place
to change the logging behavior in the future (e.g. structured output,
rate-limiting, or additional context) without touching every module.

Usage::

    from ._log import get_logger

    _LOGGER = get_logger(__name__)
"""

from __future__ import annotations

import logging


def get_logger(name: str) -> logging.Logger:
    """Return a standard :class:`logging.Logger` for *name*.

    Thin wrapper so every module in the integration obtains its logger
    from one place.  If we ever need to inject extra context, apply
    rate-limiting, or switch to structured logging, we only change this
    function.
    """
    return logging.getLogger(name)
