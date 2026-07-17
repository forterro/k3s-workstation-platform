"""Command-line interface for the workstation bootstrap generator."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from . import __version__, console, k3s, preflight
from .config import find_repo_root
from .phases import Context, run_bootstrap


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="k3s-workstation-bootstrap",
        description="Imperative seed bootstrap for the k3s AI workstation platform.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    preflight_parser = subparsers.add_parser(
        "preflight", help="Verify host and tooling prerequisites"
    )
    preflight_parser.add_argument(
        "--strict", action="store_true", help="Treat advisory warnings as failures"
    )

    bootstrap_parser = subparsers.add_parser(
        "bootstrap", help="Run the seed phases and hand off to ArgoCD"
    )
    bootstrap_parser.add_argument(
        "--skip-preflight", action="store_true", help="Skip preflight checks"
    )
    bootstrap_parser.add_argument(
        "--dry-run", action="store_true", help="List the planned phases without applying"
    )

    reset_parser = subparsers.add_parser(
        "reset", help="Completely remove k3s and its cluster (k3s-uninstall)"
    )
    reset_parser.add_argument("--dry-run", action="store_true", help="Show what would be removed")
    return parser


def _cmd_preflight(args: argparse.Namespace) -> int:
    return 0 if preflight.run(strict=args.strict) else 1


def _cmd_bootstrap(args: argparse.Namespace) -> int:
    console.banner("k3s workstation bootstrap")
    if not args.skip_preflight and not preflight.run():
        return 1

    context = Context(root=find_repo_root(), dry_run=args.dry_run)
    return 0 if run_bootstrap(context) else 1


def _cmd_reset(args: argparse.Namespace) -> int:
    console.banner("k3s workstation reset")
    k3s.uninstall(dry_run=args.dry_run)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "preflight":
        return _cmd_preflight(args)
    if args.command == "bootstrap":
        return _cmd_bootstrap(args)
    if args.command == "reset":
        return _cmd_reset(args)

    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
