"""Narrow selection and one-socket mapping; no external socket operations."""
from dataclasses import asdict
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
from types import SimpleNamespace

import pytest

from evals.voice.child_listen import ENTRYPOINT, FACTORIES, SPEC_ENV, Listener, child_spec, select_child


def selection(**changes):
    values = dict(command=[sys.executable, "-B", str(ENTRYPOINT)],
        env={"PORT": "12345", "CAMPUSPULSE_MCP_APP_FACTORY": sorted(FACTORIES)[0]},
        mode="literal-loopback", enabled=True)
    values.update(changes)
    return values


@pytest.mark.parametrize("factory", sorted(FACTORIES))
def test_only_two_exact_synthetic_factories_select(factory):
    cfg = selection(env={"PORT": "12345", "CAMPUSPULSE_MCP_APP_FACTORY": factory})
    value = select_child(**cfg)
    assert value.port == 12345 and value.factory == factory
    assert value.executable == str(Path(sys.executable).resolve())
    assert value.entrypoint == str(ENTRYPOINT)


@pytest.mark.parametrize("port", [None, "", "0", "-1", "65536", "01", " 12345", "12345 ", "1.5", 12345, True])
def test_missing_or_invalid_explicit_port_never_selects(port):
    env = selection()["env"]
    if port is None: env.pop("PORT")
    else: env["PORT"] = port
    assert select_child(**selection(env=env)) is None


@pytest.mark.parametrize("change", ["disabled", "denied", "other_mode", "interpreter", "script", "module", "code", "extra", "factory", "legacy", "no_factory"])
def test_unknown_launch_context_stays_unadapted(change):
    cfg = selection()
    if change == "disabled": cfg["enabled"] = False
    elif change in ("denied", "other_mode"): cfg["mode"] = change
    elif change == "interpreter": cfg["command"][0] = str(ENTRYPOINT.parent / "other-python.exe")
    elif change == "script": cfg["command"][2] = str(ENTRYPOINT.with_name("other.py"))
    elif change == "module": cfg["command"] = [sys.executable, "-m", "deploy.mcp-entrypoint"]
    elif change == "code": cfg["command"] = [sys.executable, "-c", "pass"]
    elif change == "extra": cfg["command"].append("--arbitrary")
    elif change == "factory": cfg["env"]["CAMPUSPULSE_MCP_APP_FACTORY"] = "arbitrary:factory"
    elif change == "legacy": cfg["env"]["CAMPUSPULSE_MCP_APP_FACTORY"] = "tests.mcp.test_full_surface:fake_legacy_production_factory"
    else: cfg["env"].pop("CAMPUSPULSE_MCP_APP_FACTORY")
    assert select_child(**cfg) is None


@pytest.mark.parametrize("tamper", [None, "marker_port", "env_port", "env_factory", "mode", "argv", "executable", "extra_key", "invalid_json"])
def test_child_revalidates_marker_against_actual_launch(tamper):
    cfg = selection()
    selected = select_child(**cfg)
    value = asdict(selected)
    env = {**cfg["env"], "W12_NETWORK_MODE": "literal-loopback"}
    argv, executable = [str(ENTRYPOINT)], sys.executable
    if tamper == "marker_port": value["port"] += 1
    if tamper == "env_port": env["PORT"] = "12346"
    if tamper == "env_factory": env["CAMPUSPULSE_MCP_APP_FACTORY"] = "arbitrary:factory"
    if tamper == "mode": env["W12_NETWORK_MODE"] = "denied"
    if tamper == "argv": argv = ["-c"]
    if tamper == "executable": executable = str(ENTRYPOINT.parent / "other-python.exe")
    if tamper == "extra_key": value["bypass"] = True
    env[SPEC_ENV] = "not-json" if tamper == "invalid_json" else json.dumps(value)
    actual = child_spec(env, argv, executable)
    assert (actual == selected) is (tamper is None)


def test_only_one_ipv4_tcp_socket_target_is_remapped():
    listener = Listener(select_child(**selection()))
    sock = SimpleNamespace(family=socket.AF_INET, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP)
    assert listener.effective_address(sock, ("0.0.0.0", 12345)) == ("127.0.0.1", 12345)
    # No second socket or repeat bind at the same tuple gets the adaptation.
    assert listener.effective_address(sock, ("0.0.0.0", 12345)) == ("0.0.0.0", 12345)
    with pytest.raises(AssertionError, match="W12 network fence"):
        sys.audit("socket.bind", None, ("0.0.0.0", 12345))


@pytest.mark.parametrize("kind", ["port", "host", "udp", "ipv6", "protocol", "float_port", "list_address"])
def test_other_requested_hosts_ports_and_sockets_keep_fence(kind):
    listener = Listener(select_child(**selection()))
    sock = SimpleNamespace(family=socket.AF_INET, type=socket.SOCK_STREAM, proto=0)
    requested = ("0.0.0.0", 12346 if kind == "port" else 12345)
    if kind == "host": requested = ("198.51.100.2", 12345)
    if kind == "udp": sock.type = socket.SOCK_DGRAM
    if kind == "ipv6": sock.family = socket.AF_INET6
    if kind == "protocol": sock.proto = socket.IPPROTO_UDP
    if kind == "float_port": requested = ("0.0.0.0", 12345.0)
    if kind == "list_address": requested = ["0.0.0.0", 12345]
    assert listener.effective_address(sock, requested) == requested and not listener.used
    with pytest.raises(AssertionError, match="W12 network fence"):
        sys.audit("socket.bind", None, requested)


def test_arbitrary_python_child_cannot_reuse_marker_or_bypass_connect_dns_fences():
    env = os.environ.copy()
    env.update(selection()["env"])
    env[SPEC_ENV] = json.dumps(asdict(select_child(**selection())))
    code = """
import os, sys
assert 'W12_SYNTHETIC_LISTEN_SPEC' not in os.environ
for event,args in [('socket.bind',(None,('0.0.0.0',12345))),
                   ('socket.connect',(None,('198.51.100.2',443))),
                   ('socket.getaddrinfo',('metadata.google.internal',80,0,0,0))]:
    try: sys.audit(event,*args)
    except AssertionError as error: assert str(error)=='W12 network fence rejected I/O'
    else: raise SystemExit(4)
print('child-fences-intact')
"""
    result = subprocess.run([sys.executable, "-I", "-B", "-c", code], env=env,
        capture_output=True, text=True, timeout=10)
    assert result.returncode == 0 and result.stdout.strip() == "child-fences-intact"
