"""traceblame-contracts: generate and check the profile JSON Schema, validate exports."""

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from traceblame_contracts.compat import breaking_within_major, diff_profiles
from traceblame_contracts.fixtures import discover_fixtures
from traceblame_contracts.gap_report import fixture_gaps, render_json, render_markdown
from traceblame_contracts.jsonschema_gen import generate_json_schema, render_json_schema
from traceblame_contracts.normalizers import NORMALIZERS, GoNormalizer, IdentityNormalizer
from traceblame_contracts.otlp import flatten_otlp_json
from traceblame_contracts.profile import load_profile
from traceblame_contracts.validate import validate_span
from traceblame_contracts.vectors import Registry, load_registry

DEFAULT_PROFILE = Path("schemas/profile/aegis-telemetry.v1.yaml")
# Directories that keep a byte-identical copy of the default profile. The normalizer embeds
# its copy with go:embed, which cannot reach outside the Go module.
DEFAULT_MIRRORS = (Path("collector/processor/normalizer/profiles"),)
DEFAULT_FIXTURES = Path("collector/testdata/fixtures")
DEFAULT_REPORT_DIR = Path(".contract")


def schema_path_for(profile_path: Path) -> Path:
    return profile_path.with_name(profile_path.name.removesuffix(".yaml") + ".schema.json")


def _mirrors(args: argparse.Namespace) -> list[Path]:
    if args.mirror is not None:
        return list(args.mirror)
    return list(DEFAULT_MIRRORS) if args.profile == DEFAULT_PROFILE else []


def _gen_schema(args: argparse.Namespace) -> int:
    profile_path: Path = args.profile
    outputs = [(schema_path_for(profile_path), render_json_schema(load_profile(profile_path)))]
    profile_text = profile_path.read_text(encoding="utf-8")
    outputs += [(mirror / profile_path.name, profile_text) for mirror in _mirrors(args)]
    if args.check:
        stale = [
            target
            for target, rendered in outputs
            if (target.read_text(encoding="utf-8") if target.is_file() else None) != rendered
        ]
        for target in stale:
            print(
                f"{target} is out of date: run `traceblame-contracts gen-schema`",
                file=sys.stderr,
            )
        if stale:
            return 1
        for target, _ in outputs:
            print(f"{target} is up to date")
        return 0
    for target, rendered in outputs:
        target.parent.mkdir(parents=True, exist_ok=True)
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
    old, new = load_profile(args.old), load_profile(args.new)
    old_vectors: Registry = load_registry(args.old_vectors) if args.old_vectors else {}
    new_vectors: Registry = load_registry(args.new_vectors) if args.new_vectors else {}
    diff = diff_profiles(old, new, old_vectors=old_vectors, new_vectors=new_vectors)
    for change in diff.changes:
        print(f"[{change.severity}] {change.rule}: {change.subject} {change.detail}".rstrip())
    print(f"required bump: {diff.bump}")
    blocked = breaking_within_major(old, new, old_vectors=old_vectors, new_vectors=new_vectors)
    if blocked:
        print(f"{len(blocked)} major change(s) need {old.profile}/{old.major + 1}")
    return 1 if blocked else 0


def _gap_report(args: argparse.Namespace) -> int:
    profile = load_profile(args.profile)
    schema = generate_json_schema(profile)
    gaps = [
        fixture_gaps(f.id, f.load(), profile, schema, IdentityNormalizer(), GoNormalizer())
        for f in discover_fixtures(args.fixtures)
    ]
    out: Path = args.out
    out.mkdir(parents=True, exist_ok=True)
    for name, text in (
        ("gap-report.md", render_markdown(gaps, profile.id, profile.revision)),
        ("gap-report.json", render_json(gaps, profile.id, profile.revision)),
    ):
        (out / name).write_bytes(text.encode("utf-8"))
        print(f"wrote {out / name}")
    raw = sum(sum(g.raw_violations.values()) for g in gaps)
    missing = sum(sum(g.missing.values()) for g in gaps)
    print(f"{len(gaps)} fixture(s): {raw} raw violation(s), {missing} missing after normalizer")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="traceblame-contracts", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    gen = sub.add_parser("gen-schema", help="generate the JSON Schema next to the profile")
    gen.add_argument("--profile", type=Path, default=DEFAULT_PROFILE)
    gen.add_argument("--check", action="store_true", help="fail if a file is out of date")
    gen.add_argument(
        "--mirror",
        type=Path,
        action="append",
        help="directory that keeps a byte-identical copy of the profile (repeatable; default "
        "for the default profile: the normalizer's embedded copy)",
    )
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
    cmp_.add_argument(
        "--old-vectors",
        type=Path,
        help="directory of golden vector files at the old revision (default: none)",
    )
    cmp_.add_argument(
        "--new-vectors",
        type=Path,
        help="directory of golden vector files at the new revision (default: none)",
    )
    cmp_.set_defaults(func=_compat)

    gap = sub.add_parser(
        "gap-report", help="report what each fixture lacks before and after the normalizer"
    )
    gap.add_argument("--fixtures", type=Path, default=DEFAULT_FIXTURES)
    gap.add_argument("--profile", type=Path, default=DEFAULT_PROFILE)
    gap.add_argument("--out", type=Path, default=DEFAULT_REPORT_DIR)
    gap.set_defaults(func=_gap_report)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
