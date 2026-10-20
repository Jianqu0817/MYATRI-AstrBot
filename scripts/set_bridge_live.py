"""Select live mode explicitly from the Live launcher; make no model call."""

from pathlib import Path
import re
import tomllib

root = Path(__file__).resolve().parents[1]
file = root / "config/bridge.local.toml"
body = file.read_text(encoding="utf-8-sig")
settings = tomllib.loads(body)["bridge"]
if not settings.get("allowed_groups"):
    raise RuntimeError("Bind the test group before selecting live mode")
if settings["mode"] == "offline":
    body, count = re.subn(r'(?m)^mode\s*=\s*"offline"', 'mode = "live"', body, count=1)
    if count != 1:
        raise RuntimeError("Invalid bridge configuration")
    temporary = file.with_suffix(".toml.tmp")
    temporary.write_text(body, encoding="utf-8")
    temporary.replace(file)
elif settings["mode"] != "live":
    raise RuntimeError("Invalid bridge mode")
print("Live mode selected. No model call made by this configuration step.")
