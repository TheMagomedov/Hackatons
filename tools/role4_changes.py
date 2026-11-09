"""Inspect fetched origin refs; record a review checkpoint only when requested.

Run ``git fetch origin --prune`` separately before this command. This tool never
fetches, checks out, merges, imports, or executes code from the inspected refs.

Save an audit with ``python tools/role4_changes.py > review.json``. After
reading those changes, acknowledge that exact snapshot with
``python tools/role4_changes.py --record-reviewed review.json``. If any fetched
ref changed since the saved report, acknowledgement fails without recording it.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

STATE_VERSION = 1
REF_PREFIX = "refs/remotes/origin/"
HEAD_REF = REF_PREFIX + "HEAD"
SHA_RE = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
REPORT_KEYS = {
    "schema_version", "checkpoint_exists", "review_recorded", "remote",
    "branches", "unchanged", "refs",
}
BRANCH_KEYS = {
    "ref", "status", "base", "head", "base_available", "commits",
    "removed_commits", "paths", "warnings",
}


class AuditError(Exception):
    """The audit cannot be completed reliably."""


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    env = dict(os.environ, GIT_OPTIONAL_LOCKS="0")
    result = subprocess.run(
        ["git", "--no-pager", "-c", "core.fsmonitor=false", *args],
        cwd=repo,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if check and result.returncode:
        message = result.stderr.decode("utf-8", errors="replace").strip()
        raise AuditError(f"git {args[0]} failed: {message}")
    return result


def _decode(data: bytes) -> str:
    return data.decode("utf-8", errors="surrogateescape")


def _refs(repo: Path) -> dict[str, str]:
    output = _decode(
        _git(repo, "for-each-ref", "--format=%(refname) %(objectname)", REF_PREFIX).stdout
    )
    refs = {}
    for line in output.splitlines():
        ref, sha = line.split(" ", 1)
        if ref != HEAD_REF:
            refs[ref] = sha
    return refs


def _state_path(repo: Path) -> Path:
    raw = _decode(_git(repo, "rev-parse", "--git-path", "role4-monitor.json").stdout).strip()
    path = Path(raw)
    return path if path.is_absolute() else repo / path


def _read_state(path: Path) -> tuple[bool, dict[str, str]]:
    if not path.exists():
        return False, {}
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise AuditError(f"Cannot read review checkpoint {path}: {exc}") from exc
    if (
        not isinstance(state, dict)
        or type(state.get("version")) is not int
        or state.get("version") != STATE_VERSION
    ):
        raise AuditError("Unsupported review checkpoint format; refusing to reset it.")
    refs = state.get("refs")
    if not _valid_refs(refs):
        raise AuditError("Invalid review checkpoint refs; refusing to reset it.")
    return True, refs


def _valid_ref(ref: object) -> bool:
    return (
        isinstance(ref, str)
        and ref.startswith(REF_PREFIX)
        and len(ref) > len(REF_PREFIX)
        and ref != HEAD_REF
    )


def _valid_sha(sha: object) -> bool:
    return isinstance(sha, str) and SHA_RE.fullmatch(sha) is not None


def _valid_refs(refs: object) -> bool:
    return isinstance(refs, dict) and all(
        _valid_ref(ref) and _valid_sha(sha) for ref, sha in refs.items()
    )


def _read_report(path: Path) -> dict[str, Any]:
    """Read an audit as JSON data; never import or execute the supplied file."""
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise AuditError(f"Cannot read reviewed JSON report {path}: {exc}") from exc
    if (
        not isinstance(report, dict)
        or report.keys() != REPORT_KEYS
        or type(report.get("schema_version")) is not int
        or report.get("schema_version") != STATE_VERSION
        or report.get("remote") != "origin"
        or not isinstance(report.get("checkpoint_exists"), bool)
        or not isinstance(report.get("review_recorded"), bool)
        or not _valid_refs(report.get("refs"))
        or not isinstance(report.get("branches"), list)
        or not isinstance(report.get("unchanged"), list)
    ):
        raise AuditError("Invalid reviewed report schema or ref hashes.")
    refs = report["refs"]
    unchanged = report["unchanged"]
    if any(not _valid_ref(ref) or ref not in refs for ref in unchanged):
        raise AuditError("Invalid unchanged refs in reviewed report.")
    represented = set(unchanged)
    if len(represented) != len(unchanged):
        raise AuditError("Duplicate refs in reviewed report.")
    for branch in report["branches"]:
        if not isinstance(branch, dict) or branch.keys() != BRANCH_KEYS:
            raise AuditError("Invalid branch schema in reviewed report.")
        ref, status = branch["ref"], branch["status"]
        base, head = branch["base"], branch["head"]
        if (
            not _valid_ref(ref)
            or ref in represented
            or status not in ("created", "updated", "rewritten", "deleted")
            or (base is not None and not _valid_sha(base))
            or (head is not None and not _valid_sha(head))
            or not isinstance(branch["base_available"], bool)
            or any(
                not isinstance(branch[key], list)
                or any(not _valid_sha(sha) for sha in branch[key])
                for key in ("commits", "removed_commits")
            )
            or any(
                not isinstance(branch[key], list)
                or any(not isinstance(value, str) or not value for value in branch[key])
                for key in ("paths", "warnings")
            )
        ):
            raise AuditError("Invalid branch data in reviewed report.")
        if status == "deleted":
            valid = base is not None and head is None and ref not in refs
        elif status == "created":
            valid = base is None and head is not None and refs.get(ref) == head
        else:
            valid = base is not None and head is not None and base != head and refs.get(ref) == head
        if not valid:
            raise AuditError("Branch heads disagree with reviewed report refs.")
        represented.add(ref)
    if not set(refs).issubset(represented):
        raise AuditError("Reviewed report omits current refs.")
    return report


def _has_commit(repo: Path, sha: str) -> bool:
    return _git(repo, "cat-file", "-e", f"{sha}^{{commit}}", check=False).returncode == 0


def _commits(repo: Path, head: str, exclude: str | None = None) -> list[str]:
    revisions = [head] + ([f"^{exclude}"] if exclude else [])
    output = _git(repo, "rev-list", "--topo-order", "--reverse", *revisions, "--").stdout
    return _decode(output).splitlines()


def _tracked_paths(repo: Path, sha: str) -> set[str]:
    output = _git(repo, "ls-tree", "-r", "--name-only", "-z", sha, "--").stdout
    return {_decode(path) for path in output.split(b"\0") if path}


def _commit_paths(repo: Path, commits: list[str]) -> set[str]:
    paths: set[str] = set()
    for sha in commits:
        # -m includes each merge parent; --root includes initial commits. Looking
        # at every commit preserves changes that a later commit reverted.
        output = _git(
            repo, "diff-tree", "--root", "--no-commit-id", "--name-only", "-r", "-m",
            "--no-renames", "--no-ext-diff", "--no-textconv", "-z", sha, "--",
        ).stdout
        paths.update(_decode(path) for path in output.split(b"\0") if path)
    return paths


def build_report(repo: Path) -> dict[str, Any]:
    """Return an audit of local remote-tracking refs without changing Git state."""
    repo = repo.resolve()
    checkpoint_exists, previous = _read_state(_state_path(repo))
    current = _refs(repo)
    branches = []
    unchanged = []
    for ref in sorted(previous.keys() | current.keys()):
        base, head = previous.get(ref), current.get(ref)
        if base == head:
            unchanged.append(ref)
            continue
        base_available = bool(base and _has_commit(repo, base))
        removed_commits: list[str] = []
        warnings = []
        if base and not base_available:
            warnings.append(
                "The checkpoint commit is unavailable locally; previous paths and "
                "removed commits cannot be fully inspected."
            )
        if head is None:
            status = "deleted"
            commits = []
            paths = _tracked_paths(repo, base) if base_available else set()
        elif base is None:
            status = "created"
            commits = _commits(repo, head)
            paths = _tracked_paths(repo, head) | _commit_paths(repo, commits)
        else:
            ancestry = (
                _git(repo, "merge-base", "--is-ancestor", base, head, check=False)
                if base_available else None
            )
            if ancestry is not None and ancestry.returncode not in (0, 1):
                raise AuditError("Cannot determine branch ancestry.")
            status = "updated" if ancestry is not None and ancestry.returncode == 0 else "rewritten"
            commits = _commits(repo, head, base if base_available else None)
            paths = _commit_paths(repo, commits)
            if status == "rewritten":
                if base_available:
                    removed_commits = _commits(repo, base, head)
                    paths |= _commit_paths(repo, removed_commits)
                    paths |= _tracked_paths(repo, base)
                paths |= _tracked_paths(repo, head)
        branches.append({
            "ref": ref,
            "status": status,
            "base": base,
            "head": head,
            "base_available": base_available,
            "commits": commits,
            "removed_commits": removed_commits,
            "paths": sorted(paths),
            "warnings": warnings,
        })
    return {
        "schema_version": STATE_VERSION,
        "checkpoint_exists": checkpoint_exists,
        "review_recorded": False,
        "remote": "origin",
        "branches": branches,
        "unchanged": unchanged,
        "refs": current,
    }


def record_reviewed(repo: Path, refs: dict[str, str]) -> None:
    """Atomically save only ref names and commit IDs after an explicit review."""
    repo = repo.resolve()
    if not _valid_refs(refs):
        raise AuditError("Invalid reviewed ref hashes.")
    path = _state_path(repo)
    _read_state(path)
    if _refs(repo) != refs:
        raise AuditError("Reviewed report is stale: remote-tracking refs changed; review again.")
    state = {"version": STATE_VERSION, "refs": refs}
    fd, name = tempfile.mkstemp(prefix="role4-monitor-", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(state, stream, ensure_ascii=True, sort_keys=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd(), help="Repository to inspect.")
    parser.add_argument(
        "--record-reviewed", type=Path, metavar="PATH",
        help="Acknowledge an already reviewed JSON report, only if its refs are still current.",
    )
    args = parser.parse_args(argv)
    try:
        if args.record_reviewed is not None:
            report = _read_report(args.record_reviewed)
            record_reviewed(args.repo, report["refs"])
            report["review_recorded"] = True
        else:
            report = build_report(args.repo)
    except (AuditError, OSError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=True), file=sys.stderr)
        return 1
    print(json.dumps(report, ensure_ascii=True, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
