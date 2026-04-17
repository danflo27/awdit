from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from paths import (
    AWDIT_DATA_ROOT_ENV,
    default_data_root,
    managed_runtime_root_names,
    migrate_legacy_runtime_layout,
    repos_root,
    resolve_data_root,
    runs_root,
    state_root,
    worktrees_root,
)


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


class PathContractTests(unittest.TestCase):
    def test_runtime_roots_follow_configured_data_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo_dir = Path(tmp_dir) / "repo"
            data_root = Path(tmp_dir) / "awdit-data"
            self.assertEqual(data_root.resolve() / "runs", runs_root(repo_dir, data_root=data_root))
            self.assertEqual(data_root.resolve() / "repos", repos_root(repo_dir, data_root=data_root))
            self.assertEqual(data_root.resolve() / "worktrees", worktrees_root(repo_dir, data_root=data_root))
            self.assertEqual(data_root.resolve() / "state", state_root(repo_dir, data_root=data_root))

    def test_resolve_data_root_prefers_env_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            custom_root = Path(tmp_dir) / "awdit-home"
            resolved = resolve_data_root(env={AWDIT_DATA_ROOT_ENV: str(custom_root)})
            self.assertEqual(custom_root.resolve(), resolved)

    def test_default_data_root_matches_project_root(self) -> None:
        project_root = Path(__file__).resolve().parents[1]
        self.assertEqual(project_root, default_data_root())

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
            data_root = Path(tmp_dir) / "awdit-data"
            _write(repo_dir / "awdit" / "data" / "runs" / "r1" / "run.json", "{}\n")
            _write(repo_dir / "awdit" / "runs" / "r2" / "run.json", "{}\n")
            _write(repo_dir / "awdit" / "repos" / "acme" / "danger_map.md", "# map\n")
            _write(repo_dir / "awdit" / "worktrees" / "r1" / "solver_1" / "file.txt", "ok\n")
            _write(repo_dir / "awdit" / "awdit.db", "sqlite\n")
            _write(repo_dir / "runs" / "r3" / "run.json", "{}\n")
            _write(repo_dir / "repos" / "beta" / "danger_map.md", "# newer map\n")
            _write(repo_dir / "state" / "awdit.db", "sqlite-newer\n")

            result = migrate_legacy_runtime_layout(repo_dir, data_root=data_root)

            self.assertTrue((runs_root(repo_dir, data_root=data_root) / "r1" / "run.json").exists())
            self.assertTrue((runs_root(repo_dir, data_root=data_root) / "r2" / "run.json").exists())
            self.assertTrue((runs_root(repo_dir, data_root=data_root) / "r3" / "run.json").exists())
            self.assertTrue((repos_root(repo_dir, data_root=data_root) / "acme" / "danger_map.md").exists())
            self.assertTrue((repos_root(repo_dir, data_root=data_root) / "beta" / "danger_map.md").exists())
            self.assertTrue((worktrees_root(repo_dir, data_root=data_root) / "r1" / "solver_1" / "file.txt").exists())
            self.assertTrue((state_root(repo_dir, data_root=data_root) / "awdit.db").exists())
            self.assertTrue(result.moved)
            self.assertFalse((repo_dir / "awdit" / "awdit.db").exists())
            self.assertFalse((repo_dir / "runs").exists())
            self.assertFalse((repo_dir / "repos").exists())

    def test_migrate_skips_when_destination_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo_dir = Path(tmp_dir) / "repo"
            data_root = Path(tmp_dir) / "awdit-data"
            _write(repo_dir / "runs" / "r1" / "run.json", "legacy\n")
            _write(data_root / "runs" / "r1" / "run.json", "new\n")

            result = migrate_legacy_runtime_layout(repo_dir, data_root=data_root)

            self.assertTrue((repo_dir / "runs" / "r1" / "run.json").exists())
            self.assertEqual("new\n", (data_root / "runs" / "r1" / "run.json").read_text(encoding="utf-8"))
            self.assertTrue(result.skipped)


if __name__ == "__main__":
    unittest.main()
