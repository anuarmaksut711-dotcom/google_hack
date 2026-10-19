"""Explicit W12-only synthetic entrypoint listener seam; never production wiring.

The actual entrypoint and app remain unchanged. Exactly one selected IPv4 TCP
socket's requested wildcard bind becomes a same-port literal-loopback OS bind.
The audit hook sees only the effective address; it NEVER admits wildcard I/O.
"""
from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import re
import socket
import sys

ENTRYPOINT = Path(__file__).resolve().parents[2] / "deploy/mcp-entrypoint.py"
FACTORIES = frozenset((
    "tests.mcp.test_read_transport:fake_entrypoint_factory",
    "tests.mcp.test_full_surface:fake_full_entrypoint_factory",
))
SPEC_ENV = "W12_SYNTHETIC_LISTEN_SPEC"
REPORT_ENV = "W12_SYNTHETIC_LISTEN_REPORT"


@dataclass(frozen=True)
class Spec:
    executable: str
    entrypoint: str
    factory: str
    port: int


def select_child(command, env, *, mode, enabled, executable=None):
    """No shell, modules, command strings, extra flags/arguments or factories."""
    executable = Path(sys.executable if executable is None else executable).resolve()
    if enabled is not True or mode != "literal-loopback" or not isinstance(command, (list, tuple)):
        return None
    if len(command) != 3 or command[1] != "-B":
        return None
    try:
        if Path(command[0]).resolve() != executable or Path(command[2]).resolve() != ENTRYPOINT:
            return None
    except (TypeError, ValueError, OSError):
        return None
    factory, port = env.get("CAMPUSPULSE_MCP_APP_FACTORY"), env.get("PORT")
    if factory not in FACTORIES or not isinstance(port, str) or not re.fullmatch(r"[1-9][0-9]{0,4}", port):
        return None
    if int(port) > 65535:
        return None
    return Spec(str(executable), str(ENTRYPOINT), factory, int(port))


def child_spec(env=None, argv=None, executable=None):
    """Revalidate startup context independently; a marker alone enables nothing."""
    env = os.environ if env is None else env
    argv = sys.argv if argv is None else argv
    executable = sys.executable if executable is None else executable
    try:
        value = json.loads(env[SPEC_ENV])
        if set(value) != {"executable", "entrypoint", "factory", "port"}:
            return None
        if type(value["port"]) is not int or len(argv) != 1:
            return None
        selected = select_child([executable, "-B", argv[0]], env,
            mode=env.get("W12_NETWORK_MODE"), enabled=True, executable=executable)
        if selected is None or asdict(selected) != value:
            return None
        return selected
    except (KeyError, TypeError, ValueError, OSError):
        return None


class Listener:
    def __init__(self, spec):
        self.spec = spec
        self.used = False

    def effective_address(self, sock, requested):
        if (not self.used and type(requested) is tuple and len(requested) == 2
                and type(requested[0]) is str and type(requested[1]) is int
                and requested == ("0.0.0.0", self.spec.port)
                and sock.family == socket.AF_INET and sock.type == socket.SOCK_STREAM
                and sock.proto in (0, socket.IPPROTO_TCP)):
            self.used = True
            return ("127.0.0.1", self.spec.port)
        return requested


def install():
    """Called at selected child startup AFTER network_guard's audit hook."""
    spec = child_spec()
    report_name = os.environ.get(REPORT_ENV)
    if spec is None or not report_name:
        return False
    report = Path(report_name)
    listener = Listener(spec)
    original = socket.socket.bind

    def bind(sock, requested):
        effective = listener.effective_address(sock, requested)
        if effective == requested:
            return original(sock, requested)
        data = dict(evidence_class="windows_local", adapter="W12_synthetic_child_listen_v1",
            factory=spec.factory, entrypoint=spec.entrypoint, executable=spec.executable,
            requested={"host": "0.0.0.0", "port": spec.port},
            effective={"host": "127.0.0.1", "port": spec.port},
            production_wildcard_bind_verified=False, status="binding")
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        try:
            result = original(sock, effective)
            host, port = sock.getsockname()
            assert (host, port) == effective
            data.update(status="bound", observed={"host": host, "port": port})
            report.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
            return result
        except BaseException:
            sock.close()
            raise
    socket.socket.bind = bind
    return True
