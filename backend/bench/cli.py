"""`bench` CLI — pack authoring utilities."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(prog="bench")
    sub = parser.add_subparsers(dest="command", required=True)

    packs = sub.add_parser("packs", help="Domain pack utilities")
    packs_sub = packs.add_subparsers(dest="packs_command", required=True)
    validate = packs_sub.add_parser("validate", help="Validate a pack directory")
    validate.add_argument("path", type=Path)

    args = parser.parse_args()
    if args.command == "packs" and args.packs_command == "validate":
        from bench.packs.loader import validate_pack

        manifest, schemas, errors = validate_pack(args.path)
        if errors:
            print(f"INVALID: {args.path}")
            for e in errors:
                print(f"  - {e}")
            sys.exit(1)
        print(f"OK: {manifest.pack}@{manifest.version}")
        print(f"  task types: {', '.join(t.slug for t in manifest.task_types)}")
        print(f"  schemas:    {', '.join(sorted(schemas))}")
        print(f"  doctrine:   {len(manifest.doctrine)} files")


if __name__ == "__main__":
    main()
