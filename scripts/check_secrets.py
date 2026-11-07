#!/usr/bin/env python3
"""Scan tracked and non-ignored project files without printing secret values."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path


SECRET_PATTERNS = (
    re.compile(rb"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b"),
    re.compile(rb"(?i)OPENAI_API_KEY\s*=\s*sk-\S+"),
    re.compile(rb"(?i)ANTHROPIC_API_KEY\s*=\s*sk-ant-\S+"),
)


def scan(root: Path) -> list[str]:
    result = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=root,
        check=True,
        stdout=subprocess.PIPE,
    )
    findings: list[str] = []
    for raw_path in result.stdout.split(b"\0"):
        if not raw_path:
            continue
        relative = Path(raw_path.decode("utf-8", errors="surrogateescape"))
        if relative.name == ".env" or (relative.name.startswith(".env.") and relative.name != ".env.example"):
            continue
        path = root / relative
        if not path.is_file():
            continue
        try:
            data = path.read_bytes()
        except OSError:
            continue
        if b"\0" in data[:4096]:
            continue
        if relative.name == ".env.example":
            # Placeholders are allowed; actual token-shaped values are not.
            data = data.replace(b"replace-with-your-openai-api-key", b"")
        if any(pattern.search(data) for pattern in SECRET_PATTERNS):
            findings.append(relative.as_posix())
    return findings


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    findings = scan(root)
    if findings:
        for filename in findings:
            print(f"Potential secret pattern in {filename} (content omitted)")
        return 1
    print("Secret scan clean; ignored .env was not scanned or displayed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
