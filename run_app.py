#!/usr/bin/env python3
"""Safe cross-platform launcher for the standalone Ringback application."""
from __future__ import annotations

import asyncio
import inspect
import os
from pathlib import Path
import sys


APP_DIR = Path(__file__).resolve().parent


def _load_env_file(path: Path) -> None:
    """Load simple dotenv assignments without executing them as shell code.

    Values already supplied by the service manager or container take priority.
    Quotes are removed only when they wrap the complete value. Variables such
    as ``$HOME`` and previously loaded keys are expanded after parsing.
    """
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key or not key.replace("_", "a").isalnum() or key[0].isdigit():
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        value = os.path.expanduser(os.path.expandvars(value))
        os.environ.setdefault(key, value)


def _prepare_runtime() -> None:
    _load_env_file(APP_DIR / ".env")

    scripts_dir = APP_DIR / ".venv" / ("Scripts" if os.name == "nt" else "bin")
    path_parts = [str(scripts_dir)]
    if os.name != "nt":
        path_parts.append(str(Path.home() / ".local" / "bin"))
        if sys.platform == "darwin":
            path_parts.append("/opt/homebrew/bin")
    current_path = os.environ.get("PATH", "")
    if current_path:
        path_parts.append(current_path)
    os.environ["PATH"] = os.pathsep.join(path_parts)

    app_path = str(APP_DIR)
    if app_path not in sys.path:
        sys.path.insert(0, app_path)


def main() -> int:
    _prepare_runtime()
    from standalone_app import main as application_main

    result = application_main()
    if inspect.isawaitable(result):
        result = asyncio.run(result)
    return int(result or 0)


if __name__ == "__main__":
    raise SystemExit(main())
