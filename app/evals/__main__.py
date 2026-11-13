"""Run the offline synthetic suite: python -m app.evals --output var/evals/report.json."""

import argparse
import asyncio
import json
import os
import stat
import sys
import tempfile
from pathlib import Path

from .runner import run_evaluations


def write_report(path: Path, report: dict) -> None:
    content = (json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
    if len(content) > 200_000:
        raise ValueError("Evaluation report exceeds its size limit")
    path = Path(os.path.abspath(path))
    for component in [*reversed(path.parents), path]:
        try:
            info = component.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("Evaluation output must not traverse links")
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="wb", dir=path.parent, prefix=".eval-", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(content)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("var/evals/report.json"))
    args = parser.parse_args(argv)
    try:
        report = asyncio.run(run_evaluations())
        write_report(args.output, report)
    except Exception as error:
        print(f"Evaluation did not finish ({type(error).__name__}); no private details logged.", file=sys.stderr)
        return 2
    print(f"Synthetic checks: {report['passed']}/{report['total']} passed; failed={report['failed']}.")
    return 0 if report["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
