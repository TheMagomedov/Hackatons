"""Exercise Git audits against local temporary repositories, without a network."""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "tools" / "role4_changes.py"


class GitChangesTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.repo = Path(self.temporary.name)
        self.git("init", "-q")
        self.git("config", "user.name", "Audit Test")
        self.git("config", "user.email", "audit@example.invalid")
        self.initial = self.commit_file("initial.txt", "initial\n", "Initial commit")
        self.git("update-ref", "refs/remotes/origin/main", self.initial)
        self.git("symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
        self.state = self.repo / ".git" / "role4-monitor.json"

    def git(self, *args):
        result = subprocess.run(
            ["git", *args], cwd=self.repo, check=True, capture_output=True, text=True,
        )
        return result.stdout.strip()

    def commit_file(self, name, content, message):
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        self.git("add", "--", name)
        self.git("commit", "-qm", message)
        return self.git("rev-parse", "HEAD")

    def audit(self, *args, success=True):
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--repo", str(self.repo), *args],
            check=False, capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0 if success else 1, result.stderr)
        return json.loads(result.stdout if success else result.stderr)

    def save_report(self, report):
        path = self.repo / "review.json"
        path.write_text(json.dumps(report), encoding="utf-8")
        return str(path)

    def checkpoint(self):
        path = self.save_report(self.audit())
        report = self.audit("--record-reviewed", path)
        self.assertTrue(report["review_recorded"])
        return report

    def test_first_audit_lists_every_branch_and_is_read_only(self):
        feature = self.commit_file("feature.txt", "feature\n", "Feature")
        self.git("update-ref", "refs/remotes/origin/feature", feature)
        first = self.audit()
        second = self.audit()
        self.assertEqual(first, second)
        self.assertFalse(self.state.exists())
        self.assertFalse(first["checkpoint_exists"])
        self.assertEqual(len(first["branches"]), 2)
        self.assertNotIn("refs/remotes/origin/HEAD", first["refs"])
        for branch in first["branches"]:
            self.assertEqual(branch["status"], "created")
            self.assertIsNone(branch["base"])
            self.assertIn("initial.txt", branch["paths"])
        by_ref = {branch["ref"]: branch for branch in first["branches"]}
        self.assertIn("feature.txt", by_ref["refs/remotes/origin/feature"]["paths"])

    def test_explicit_checkpoint_contains_only_refs_and_suppresses_reviewed_changes(self):
        self.checkpoint()
        state = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual(state, {"version": 1, "refs": {"refs/remotes/origin/main": self.initial}})
        before = self.state.read_bytes()
        report = self.audit()
        self.assertTrue(report["checkpoint_exists"])
        self.assertEqual(report["branches"], [])
        self.assertEqual(report["unchanged"], ["refs/remotes/origin/main"])
        self.assertEqual(self.state.read_bytes(), before)

    def test_reports_paths_even_when_later_commit_reverts_them(self):
        self.checkpoint()
        added = self.commit_file("transient file.txt", "temporary\n", "Add temporary file")
        self.git("revert", "--no-edit", added)
        reverted = self.git("rev-parse", "HEAD")
        self.git("update-ref", "refs/remotes/origin/main", reverted)
        before = self.state.read_bytes()
        branch = self.audit()["branches"][0]
        self.assertEqual(branch["status"], "updated")
        self.assertEqual(branch["base"], self.initial)
        self.assertEqual(branch["head"], reverted)
        self.assertEqual(branch["commits"], [added, reverted])
        self.assertIn("transient file.txt", branch["paths"])
        self.assertEqual(self.state.read_bytes(), before)

    def test_reports_deleted_branch_without_forgetting_it_until_checkpoint(self):
        self.checkpoint()
        self.git("update-ref", "-d", "refs/remotes/origin/main")
        report = self.audit()
        branch = report["branches"][0]
        self.assertEqual(branch["status"], "deleted")
        self.assertEqual(branch["base"], self.initial)
        self.assertIsNone(branch["head"])
        self.assertEqual(branch["commits"], [])
        self.assertIn("initial.txt", branch["paths"])
        self.assertEqual(self.audit(), report)
        self.checkpoint()
        self.assertEqual(self.audit()["branches"], [])

    def test_rewritten_branch_includes_added_and_removed_commit_paths(self):
        old = self.commit_file("removed.txt", "old branch\n", "Old branch change")
        self.git("update-ref", "refs/remotes/origin/main", old)
        self.checkpoint()
        self.git("reset", "--hard", self.initial)
        new = self.commit_file("replacement.txt", "new branch\n", "Replacement change")
        self.git("update-ref", "refs/remotes/origin/main", new)
        branch = self.audit()["branches"][0]
        self.assertEqual(branch["status"], "rewritten")
        self.assertEqual(branch["commits"], [new])
        self.assertEqual(branch["removed_commits"], [old])
        self.assertTrue(branch["base_available"])
        self.assertTrue({"removed.txt", "replacement.txt"}.issubset(branch["paths"]))

    def test_corrupt_checkpoint_fails_without_overwriting(self):
        path = self.save_report(self.audit())
        self.state.write_text("not JSON", encoding="utf-8")
        result = self.audit("--record-reviewed", path, success=False)
        self.assertIn("checkpoint", result["error"])
        self.assertEqual(self.state.read_text(encoding="utf-8"), "not JSON")

    def test_missing_checkpoint_object_is_reported_as_incomplete(self):
        self.state.write_text(json.dumps({
            "version": 1,
            "refs": {"refs/remotes/origin/main": "0" * 40},
        }), encoding="utf-8")
        branch = self.audit()["branches"][0]
        self.assertEqual(branch["status"], "rewritten")
        self.assertFalse(branch["base_available"])
        self.assertEqual(branch["commits"], [self.initial])
        self.assertIn("initial.txt", branch["paths"])
        self.assertTrue(branch["warnings"])

    def test_stale_review_cannot_hide_unseen_commits(self):
        self.checkpoint()
        added_a = self.commit_file("a.txt", "reviewed\n", "A")
        self.git("update-ref", "refs/remotes/origin/main", added_a)
        path = self.save_report(self.audit())
        before = self.state.read_bytes()
        added_b = self.commit_file("b.txt", "unseen\n", "B")
        self.git("update-ref", "refs/remotes/origin/main", added_b)
        error = self.audit("--record-reviewed", path, success=False)
        self.assertIn("stale", error["error"])
        self.assertEqual(self.state.read_bytes(), before)
        pending = self.audit()["branches"][0]
        self.assertEqual(pending["head"], added_b)
        self.assertEqual(pending["commits"], [added_a, added_b])
        self.assertIn("b.txt", pending["paths"])

    def test_malformed_report_is_rejected_without_creating_checkpoint(self):
        valid = self.audit()
        variants = []
        for key, value in (
            ("schema_version", True),
            ("schema_version", 99),
            ("remote", "upstream"),
            ("refs", {"refs/remotes/origin/main": "not-a-sha"}),
            ("refs", {"refs/heads/main": self.initial}),
            ("branches", []),
        ):
            variants.append(dict(valid, **{key: value}))
        variants.append({"refs": valid["refs"]})
        for report in variants:
            with self.subTest(report=report):
                path = self.save_report(report)
                self.audit("--record-reviewed", path, success=False)
                self.assertFalse(self.state.exists())

    def test_report_path_is_read_as_data_without_executing_python(self):
        marker = self.repo / "executed.txt"
        path = self.repo / "not_a_report.py"
        path.write_text(f"open({str(marker)!r}, 'w').write('executed')", encoding="utf-8")
        error = self.audit("--record-reviewed", str(path), success=False)
        self.assertIn("JSON report", error["error"])
        self.assertFalse(marker.exists())
        self.assertFalse(self.state.exists())


if __name__ == "__main__":
    unittest.main()
