"""Read-only .env parser. It never writes or mutates the source file."""

from __future__ import annotations

from pathlib import Path


def read_env_file(path: Path) -> dict[str, str]:
    """Read simple KEY=value dotenv syntax without changing the file."""
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        name, raw_value = line.split("=", 1)
        name = name.strip()
        value = raw_value.strip()
        if not name or not name.replace("_", "A").isalnum() or name[0].isdigit():
            continue
        if value and value[0] in ('"', "'"):
            quote = value[0]
            end = value.find(quote, 1)
            value = value[1:end] if end >= 0 else value[1:]
        else:
            value = value.split(" #", 1)[0].strip()
        values[name] = value
    return values
