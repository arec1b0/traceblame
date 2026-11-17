"""The fault run's id, computed once (#14, PR #64): `new` writes .tools/faults/<run>/run.json
for this checkout's commit and the configured seed and prints the id; every phase and the
report then resolve the run through that file. `new --plan <name>` creates a run of a committed
plan file (#70; tools/faults/plans/), always a new one. `resume` prints the id of an existing
run after checking that it exists, is of the given plan, and that its plan file is unchanged.
`adopt` writes run.json for a run recorded before the file existed, from its apps' state.json.
"""

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import faultslib  # noqa: E402
from kindlib import is_dirty, require_clean  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=cast("str", __doc__).splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    new = sub.add_parser("new")
    new.add_argument("--plan", default=faultslib.VALIDATION, help="tools/faults/plans/<name>.yaml")
    resume = sub.add_parser("resume")
    resume.add_argument("--run-id", required=True)
    resume.add_argument("--plan", help="the plan the run must be of")
    sub.add_parser("adopt").add_argument("--run-id", required=True)
    args = parser.parse_args()
    if args.command == "new":
        seed = int(faultslib.config(args.plan)["seed"])
        commit = require_clean(faultslib.ROOT)
        dirty = is_dirty(faultslib.ROOT)
        print(
            faultslib.new_run(
                seed=seed, commit=commit, dirty=dirty, now=datetime.now(UTC), plan_name=args.plan
            )
        )
    elif args.command == "resume":
        print(faultslib.resume_run(args.run_id, args.plan))
    else:
        print(faultslib.adopt_run(args.run_id)["run_id"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
