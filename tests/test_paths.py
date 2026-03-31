from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from paths import (
    managed_runtime_root_names,
    migrate_legacy_runtime_layout,
    repos_root,
    runs_root,
    state_root,
    worktrees_root,
)


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


class PathContractTests(unittest.TestCase):
    def test_runtime_roots_are_repo_level_peers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo_dir = Path(tmp_dir) / "repo"
            self.assertEqual(repo_dir / "runs", runs_root(repo_dir))
            self.assertEqual(repo_dir / "repos", repos_root(repo_dir))
            self.assertEqual(repo_dir / "worktrees", worktrees_root(repo_dir))
            self.assertEqual(repo_dir / "state", state_root(repo_dir))

    def test_managed_runtime_root_names_include_legacy_root(self) -> None:
        roots = managed_runtime_root_names(include_legacy=True)
        self.assertIn("runs", roots)
        self.assertIn("repos", roots)
        self.assertIn("worktrees", roots)
        self.assertIn("state", roots)
        self.assertIn("awdit", roots)

    def test_migrate_legacy_runtime_layout_moves_legacy_locations(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo_dir = Path(tmp_dir) / "repo"
            _write(repo_dir / "awdit" / "data" / "runs" / "r1" / "run.json", "{}\n")
            _write(repo_dir / "awdit" / "runs" / "r2" / "run.json", "{}\n")
            _write(repo_dir / "awdit" / "repos" / "acme" / "danger_map.md", "# map\n")
            _write(repo_dir / "awdit" / "worktrees" / "r1" / "solver_1" / "file.txt", "ok\n")
            _write(repo_dir / "awdit" / "awdit.db", "sqlite\n")

            result = migrate_legacy_runtime_layout(repo_dir)

            self.assertTrue((runs_root(repo_dir) / "r1" / "run.json").exists())
            self.assertTrue((runs_root(repo_dir) / "r2" / "run.json").exists())
            self.assertTrue((repos_root(repo_dir) / "acme" / "danger_map.md").exists())
            self.assertTrue((worktrees_root(repo_dir) / "r1" / "solver_1" / "file.txt").exists())
            self.assertTrue((state_root(repo_dir) / "awdit.db").exists())
            self.assertTrue(result.moved)
            self.assertFalse((repo_dir / "awdit" / "awdit.db").exists())

    def test_migrate_skips_when_destination_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo_dir = Path(tmp_dir) / "repo"
            _write(repo_dir / "awdit" / "data" / "runs" / "r1" / "run.json", "legacy\n")
            _write(repo_dir / "runs" / "r1" / "run.json", "new\n")

            result = migrate_legacy_runtime_layout(repo_dir)

            self.assertTrue((repo_dir / "awdit" / "data" / "runs" / "r1" / "run.json").exists())
            self.assertEqual("new\n", (repo_dir / "runs" / "r1" / "run.json").read_text(encoding="utf-8"))
            self.assertTrue(result.skipped)


if __name__ == "__main__":
    unittest.main()
