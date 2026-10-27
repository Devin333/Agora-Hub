from __future__ import annotations

import argparse
import json
from typing import Any, Sequence

from interfaces.cli.commands.sources import build_source_runtime_provider
from interfaces.composition.runtime_execution import build_process_execution_composition
from interfaces.cli.commands import (
    accounts,
    api,
    artifacts,
    dev,
    diagnose,
    entities,
    events,
    mcp,
    memory,
    paper,
    reports,
    runs,
    schedules,
    sources,
    storage,
    subscriptions,
    tools,
    workers,
    waits,
)


COMMAND_MODULES = (
    dev,
    reports,
    subscriptions,
    entities,
    events,
    api,
    workers,
    schedules,
    waits,
    memory,
    diagnose,
    storage,
    sources,
    runs,
    artifacts,
    tools,
    mcp,
    paper,
    accounts,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="news", description="Agora Hub command line interface")
    parser.set_defaults(source_runtime_provider=build_source_runtime_provider())
    parser.set_defaults(runtime_execution_composition=build_process_execution_composition())
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command_module in COMMAND_MODULES:
        command_module.register(subparsers)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.handler(args)


def print_json(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))


__all__ = [
    "COMMAND_MODULES",
    "build_parser",
    "main",
    "print_json",
]


if __name__ == "__main__":
    raise SystemExit(main())
