"""Command line entry point for source validation and release artifacts."""

import argparse
import json
import sys

from .package import ReleaseError, build, validate_source, verify


def main(argv=None):
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments and arguments[0] == "native":
        from .native import main as native_main
        return native_main(arguments[1:])
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="operation", required=True)
    validate_parser = subcommands.add_parser("validate", help="Read-only source validation")
    validate_parser.add_argument("source")
    build_parser = subcommands.add_parser("build", help="Build immutable deterministic release artifacts")
    build_parser.add_argument("source")
    build_parser.add_argument("--output", required=True)
    verify_parser = subcommands.add_parser("verify", help="Verify ZIP, manifest, checksums and extracted bytes")
    verify_parser.add_argument("archive")
    verify_parser.add_argument("--manifest", required=True)
    verify_parser.add_argument("--checksum", required=True)
    verify_parser.add_argument("--extract-to")
    subcommands.add_parser("native", help="Read-only Codex plugin/read or installed discovery checks")
    args = parser.parse_args(arguments)
    try:
        if args.operation == "validate":
            result = validate_source(args.source)
        elif args.operation == "build":
            result = build(args.source, args.output)
        else:
            result = verify(args.archive, args.manifest, args.checksum, args.extract_to)
    except (ReleaseError, OSError, ValueError, TypeError) as error:
        print(f"Release validation failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
