"""Install W12 fence in subprocesses only when explicitly requested by runner."""
import os
if os.environ.get("W12_NETWORK_MODE"):
    import network_guard
    if os.environ.get("W12_SYNTHETIC_LISTEN_SPEC"):
        from child_listen import install
        install()
