"""Query public OSV for the exact PyPI name/version pairs in product locks."""

import json
from pathlib import Path
import re
from urllib.request import Request, urlopen


def main() -> None:
    root = Path(__file__).resolve().parent
    pairs = sorted({
        (name, version)
        for lock in root.glob("*.lock")
        for name, version in re.findall(r"^([\w.-]+)==([^ ;\\\n]+)", lock.read_text(), re.M)
    })
    request = Request(
        "https://api.osv.dev/v1/querybatch",
        data=json.dumps({"queries": [
            {"package": {"name": name, "ecosystem": "PyPI"}, "version": version}
            for name, version in pairs
        ]}).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urlopen(request, timeout=45) as response:
        results = json.load(response)["results"]
    if len(results) != len(pairs):
        raise RuntimeError("Incomplete advisory response")
    findings = [
        {"package": pair, "advisories": result["vulns"]}
        for pair, result in zip(pairs, results) if result.get("vulns")
    ]
    print(json.dumps({"queried": len(pairs), "findings": findings}, sort_keys=True))
    if findings:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
