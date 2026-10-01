import argparse
import json
import sys
from pathlib import Path
from jsonschema.exceptions import ValidationError

from .configuration import compile_preview
from .datasets import inspect_candidates


def read(path):
    return json.loads(Path(path).read_text())


def main():
    parser = argparse.ArgumentParser(description="Offline phase-0 tools; no training submission")
    sub = parser.add_subparsers(dest="command", required=True)
    preview = sub.add_parser("preview")
    preview.add_argument("--bundle", required=True)
    preview.add_argument("--overrides", required=True)
    preview.add_argument("--managed", required=True)
    inspect = sub.add_parser("inspect-data")
    inspect.add_argument("--root", required=True)
    inspect.add_argument("--contract", required=True)
    args = parser.parse_args()
    try:
        if args.command == "preview":
            result = compile_preview(read(args.bundle), read(args.overrides), read(args.managed))
        else:
            result = inspect_candidates(args.root, read(args.contract))
        print(json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False))
    except (ValueError, OSError, KeyError, ValidationError) as exc:
        parser.exit(2, f"error: {exc}\n")


if __name__ == "__main__":
    main()
