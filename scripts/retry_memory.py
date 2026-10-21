"""Show private local health; --apply releases paused summaries via the running owner."""
import argparse
from pathlib import Path
import re
import tomllib

import httpx


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="Release pending summary backoff; background API work will resume")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    try:
        settings = tomllib.loads((root / "config/bridge.local.toml").read_text(encoding="utf-8-sig"))["bridge"]
        token = (root / settings["token_file"]).read_text(encoding="ascii").strip()
        with httpx.Client(base_url=f"http://127.0.0.1:{int(settings.get('port', 8766))}",
                headers={"Authorization": "Bearer " + token}, timeout=20, trust_env=False,
                follow_redirects=False) as client:
            response = client.get("/v1/diagnostics")
            response.raise_for_status()
            report = response.json()
            for item in report.get("health", {}).get("alerts", []):
                print(item["code"], item["count"])
            if not args.apply:
                print("Read only. Add --apply to resume pending summaries; this permits background model calls.")
                return
            total = 0
            for group in report.get("groups", []):
                scope = group.get("scope", "")
                if not re.fullmatch(r"[0-9a-f]{32}", scope):
                    continue
                response = client.post("/v1/admin/retry-summaries", json={"scope": scope})
                response.raise_for_status()
                total += response.json().get("windows", 0)
            print(f"Summary windows resumed: {total}. Check Monitor-Context.cmd for progress.")
    except (OSError, ValueError, KeyError, httpx.HTTPError) as error:
        print(f"Memory recovery unavailable ({type(error).__name__}). Start the updated WeChat bridge first; no automatic resend.")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
