"""Is the internet (more exactly: Safaricom) reachable right now? Standard library only.

WHY THIS EXISTS: when the internet drops, the shop must keep selling. Cash sales never need the internet, but M-Pesa does.
The cashier screen asks the server "are we online?" every few seconds, so it can show a clear Online / No internet label and
steer the cashier to cash instead of letting them wait for an M-Pesa prompt that can never arrive.

HOW IT CHECKS: it tries to open a connection to Safaricom's Daraja address (the one M-Pesa actually uses), so the answer
means "M-Pesa is reachable", not just "something is online". The answer is remembered for a few seconds so the page can ask often.
TO CHECK A DIFFERENT ADDRESS: set SMARTPOS_ONLINE_CHECK=host:port in the .env file."""
import os
import socket
import time
from urllib.parse import urlparse

import mpesa

_cache = {"t": 0.0, "ok": True}  # the last answer and when we got it


def target():
    """The (host, port) we try to reach."""
    spec = os.environ.get("SMARTPOS_ONLINE_CHECK", "").strip()
    if spec:
        host, _, port = spec.partition(":")
        return host, int(port or 443)
    return urlparse(mpesa.base()).hostname, 443  # Safaricom's test or live address, depending on MPESA_MODE


def online(max_age=15):
    """True if the target answered recently. A fresh check is made at most once every max_age seconds."""
    now = time.time()
    if now - _cache["t"] < max_age:
        return _cache["ok"]
    try:
        socket.create_connection(target(), timeout=2).close()
        ok = True
    except (OSError, ValueError):
        ok = False
    _cache.update(t=now, ok=ok)
    return ok
