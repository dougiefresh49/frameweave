"""Launcher. ``--version`` works today; other commands arrive with issue #15."""

from __future__ import annotations

import argparse
import sys

from frameweave import __version__


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="frameweave", add_help=False)
    parser.add_argument("--version", action="store_true")
    parsed, _unknown = parser.parse_known_args(argv)
    if parsed.version:
        print(f"frameweave {__version__}")
        raise SystemExit(0)
    try:
        from frameweave.cli import main as cli_main
    except ModuleNotFoundError as exc:
        if exc.name == "frameweave.cli":
            print(
                "frameweave: commands arrive with issue #15; only --version works today",
                file=sys.stderr,
            )
            raise SystemExit(2) from None
        raise
    forwarded = sys.argv[1:] if argv is None else argv
    raise SystemExit(cli_main(forwarded))


if __name__ == "__main__":
    main()
