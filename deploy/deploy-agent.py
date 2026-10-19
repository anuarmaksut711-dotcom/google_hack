"""T13 offline Prepare entrypoint. Cloud execution is disabled in this phase."""
import argparse
import json
from pathlib import Path
import sys


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--execute", action="store_true")
    options = parser.parse_args(argv)
    try:
        # Unconditional code gate: JSON/env/model cannot lift LOCAL_ONLY.
        if options.execute:
            raise ValueError("LOCAL_ONLY_execute_forbidden")
        if options.config is None:
            raise ValueError("explicit_config_file_required")
        raw = json.loads(options.config.read_text(encoding="utf-8-sig"))
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
        from campuspulse_recovery.agent.smoke_agent import prepare_agent_request
        result = prepare_agent_request(raw)
    except (ValueError, OSError):
        print(json.dumps({"status": "BLOCKED", "policy": "LOCAL_ONLY",
                          "reason": "execute_forbidden" if options.execute else "prerequisites_required",
                          "cloud_calls": 0}))
        return 2
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
