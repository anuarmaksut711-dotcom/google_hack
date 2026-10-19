"""Irreversible audit fence shared with child Python processes; no credentials."""
import os
import sys

COUNTS = {"allowed_loopback": 0, "denied": 0}
BLOCKED_EVENTS = {}
MODE = os.environ.get("W12_NETWORK_MODE", "denied")
if MODE not in ("denied", "literal-loopback"):
    raise RuntimeError("W12 explicit network mode required")


def guard(event, args):
    if event in ("socket.connect", "socket.bind", "socket.sendto", "socket.getaddrinfo"):
        if event == "socket.getaddrinfo":
            host = args[0]
        else:
            address = args[-1]
            host = address[0] if isinstance(address, tuple) else None
        if MODE == "literal-loopback" and host in ("127.0.0.1", "::1"):
            COUNTS["allowed_loopback"] += 1
            return
        COUNTS["denied"] += 1
        BLOCKED_EVENTS[event] = BLOCKED_EVENTS.get(event, 0) + 1
        raise AssertionError("W12 network fence rejected I/O")


sys.addaudithook(guard)
