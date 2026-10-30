"""traceblame-contracts: generate and check the profile JSON Schema, validate exports."""

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from traceblame_contracts.compat import diff_profiles
from traceblame_contracts.jsonschema_gen import generate_json_schema, render_json_schema
from traceblame_contracts.normalizers import NORMALIZERS
from traceblame_contracts.otlp import flatten_otlp_json
from traceblame_contracts.profile import load_profile
from traceblame_contracts.validate import validate_span

DEFAULT_PROFILE = Path("schemas/profile/aegis-telemetry.v1.yaml")


def schema_path_for(profile_path: Path) -> Path:
    return profile_path.with_name(profile_path.name.removesuffix(".yaml") + ".schema.json")


def _gen_schema(args: argparse.Namespace) -> int:
    profile_path: Path = args.profile
    target = schema_path_for(profile_path)
    rendered = render_json_schema(load_profile(profile_path))
    if args.check:
        current = target.read_text(encoding="utf-8") if target.is_file() else None
        if current != rendered:
            print(
                f"{target} is out of date: run `traceblame-contracts gen-schema`",
                file=sys.stderr,
            )
            return 1
        print(f"{target} is up to date")
        return 0
    target.write_bytes(rendered.encode("utf-8"))
    print(f"wrote {target}")
    return 0


def _validate(args: argparse.Namespace) -> int:
    schema = generate_json_schema(load_profile(args.profile))
    normalizer = next(n for n in NORMALIZERS if n.name == args.normalizer)
    doc = json.loads(Path(args.otlp_json).read_text(encoding="utf-8"))
    failures = 0
    for span in flatten_otlp_json(normalizer.normalize(doc)):
        for violation in validate_span(span, schema):
            failures += 1
            print(f"{span['span_id']} {span['name']!r}: {violation.path}: {violation.message}")
    print(f"{failures} violation(s)")
    return 1 if failures else 0


def _compat(args: argparse.Namespace) -> int:
    breaks = diff_profiles(load_profile(args.old), load_profile(args.new))
    for brk in breaks:
        print(f"{brk.rule}: {brk.subject} {brk.detail}".rstrip())
    print(f"{len(breaks)} breaking change(s)")
    return 1 if breaks else 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="traceblame-contracts", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    gen = sub.add_parser("gen-schema", help="generate the JSON Schema next to the profile")
    gen.add_argument("--profile", type=Path, default=DEFAULT_PROFILE)
    gen.add_argument("--check", action="store_true", help="fail if the file is out of date")
    gen.set_defaults(func=_gen_schema)

    val = sub.add_parser("validate", help="validate an OTLP/JSON export against the profile")
    val.add_argument("otlp_json", type=Path)
    val.add_argument("--profile", type=Path, default=DEFAULT_PROFILE)
    val.add_argument(
        "--normalizer", choices=[n.name for n in NORMALIZERS], default=NORMALIZERS[0].name
    )
    val.set_defaults(func=_validate)

    cmp_ = sub.add_parser("compat", help="list breaking changes between two profile files")
    cmp_.add_argument("old", type=Path)
    cmp_.add_argument("new", type=Path)
    cmp_.set_defaults(func=_compat)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
