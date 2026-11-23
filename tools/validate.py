"""Lightweight validation that does not require Home Assistant to be installed."""

from __future__ import annotations

import ast
import json
from pathlib import Path

ROOT = Path(__file__).parents[1]
COMPONENT = ROOT / "custom_components" / "evohome_optimum_start"

required = {
    "__init__.py",
    "manifest.json",
    "config_flow.py",
    "controller.py",
    "const.py",
    "entity.py",
    "sensor.py",
    "binary_sensor.py",
    "switch.py",
    "button.py",
    "learning.py",
    "strings.json",
    "translations/en.json",
}
missing = sorted(path for path in required if not (COMPONENT / path).is_file())
if missing:
    raise SystemExit(f"Missing required files: {', '.join(missing)}")

for path in sorted(COMPONENT.glob("*.py")):
    ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

json_files = [
    COMPONENT / "manifest.json",
    COMPONENT / "strings.json",
    COMPONENT / "translations/en.json",
    ROOT / "hacs.json",
]
for path in json_files:
    json.loads(path.read_text(encoding="utf-8"))

strings = json.loads((COMPONENT / "strings.json").read_text(encoding="utf-8"))
english = json.loads((COMPONENT / "translations/en.json").read_text(encoding="utf-8"))
if strings != english:
    raise SystemExit("strings.json and translations/en.json differ")

manifest = json.loads((COMPONENT / "manifest.json").read_text(encoding="utf-8"))
if manifest.get("domain") != "evohome_optimum_start":
    raise SystemExit("Unexpected manifest domain")
if manifest.get("config_flow") is not True:
    raise SystemExit("Config flow is not enabled")

print(f"Validated {len(required)} required files, Python syntax, and JSON metadata.")

