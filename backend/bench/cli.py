"""`bench` CLI — pack authoring utilities."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path


def _validate(path: Path) -> None:
    from bench.packs.loader import validate_pack

    manifest, schemas, errors = validate_pack(path)
    if errors:
        print(f"INVALID: {path}")
        for e in errors:
            print(f"  - {e}")
        sys.exit(1)
    print(f"OK: {manifest.pack}@{manifest.version}")
    print(f"  task types: {', '.join(t.slug for t in manifest.task_types)}")
    print(f"  schemas:    {', '.join(sorted(schemas))}")
    print(f"  doctrine:   {len(manifest.doctrine)} files")
    print(f"  methods:    {', '.join(m.slug for m in manifest.methods) or '(none)'} "
          "— static safety scan passed (a deterrent, not a sandbox)")


def _hash(path: Path) -> None:
    from bench.packs.integrity import iter_pack_files, pack_content_hash

    if not path.is_dir():
        print(f"not a directory: {path}")
        sys.exit(1)
    files = iter_pack_files(path)
    print(pack_content_hash(path))
    print(f"  {len(files)} files under {path}", file=sys.stderr)
    print(
        "  This is the hash an install pins on the pack row. Reinstall the pack "
        "(restart bench, or POST /api/packs/install) to re-pin after an intentional edit.",
        file=sys.stderr,
    )


def main() -> None:
    parser = argparse.ArgumentParser(prog="bench")
    sub = parser.add_subparsers(dest="command", required=True)

    packs = sub.add_parser("packs", help="Domain pack utilities")
    packs_sub = packs.add_subparsers(dest="packs_command", required=True)
    validate = packs_sub.add_parser("validate", help="Validate a pack directory")
    validate.add_argument("path", type=Path)
    hash_cmd = packs_sub.add_parser(
        "hash", help="Print the pack content hash used for integrity pinning"
    )
    hash_cmd.add_argument("path", type=Path)

    args = parser.parse_args()
    if args.command == "packs":
        if args.packs_command == "validate":
            _validate(args.path)
        elif args.packs_command == "hash":
            _hash(args.path)


if __name__ == "__main__":
    main()
