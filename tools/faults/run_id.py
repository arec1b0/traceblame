"""The fault run's id, computed once (#14, PR #64): `new` writes .tools/faults/<run>/run.json
for this checkout's commit and the configured seed and prints the id; every phase and the
report then resolve the run through that file. `adopt` writes run.json for a run recorded
before the file existed, from its apps' state.json.
"""

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import faultslib  # noqa: E402
from kindlib import require_clean  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=cast("str", __doc__).splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("new")
    sub.add_parser("adopt").add_argument("--run-id", required=True)
    args = parser.parse_args()
    if args.command == "new":
        seed = int(faultslib.config()["seed"])
        commit = require_clean(faultslib.ROOT)
        print(faultslib.new_run(seed=seed, commit=commit, now=datetime.now(UTC)))
    else:
        print(faultslib.adopt_run(args.run_id)["run_id"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
