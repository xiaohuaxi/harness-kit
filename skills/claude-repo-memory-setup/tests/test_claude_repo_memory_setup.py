#!/usr/bin/env python3
"""Self-contained isolated unit tests for claude-repo-memory-setup.sh.

Uses Python 3.8+ standard library only. Every test operates inside its own
tempfile.TemporaryDirectory() with isolated HOME / CLAUDE_HOME / TMPDIR /
Git config and never touches real user directories.
"""
from __future__ import annotations

import os
import platform
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Dict, List, Optional, Tuple

SKILL_DIR = Path(__file__).resolve().parent.parent
SCRIPT_PATH = SKILL_DIR / "claude-repo-memory-setup.sh"

HAS_REAL_SVN = bool(shutil.which("svn") and shutil.which("svnadmin"))

SCRUB_ENV_KEYS = (
    "CLAUDE_HOME",
    "CLAUDE_CONFIG_DIR",
    "CLAUDE_CODE_PROJECT_DIR_NAME",
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_COMMON_DIR",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_CONFIG_GLOBAL",
    "GIT_AUTHOR_NAME",
    "GIT_AUTHOR_EMAIL",
    "GIT_COMMITTER_NAME",
    "GIT_COMMITTER_EMAIL",
    "EMAIL",
    "CDPATH",
)


def select_bash_bin() -> str:
    override = os.environ.get("BASH_BIN")
    if override:
        return override
    if platform.system() == "Darwin" and os.path.exists("/bin/bash"):
        return "/bin/bash"
    return shutil.which("bash") or "/bin/bash"


def detect_utf8_locale(bash_bin: str) -> str:
    for candidate in ("C.UTF-8", "en_US.UTF-8", "UTF-8"):
        env = dict(os.environ)
        env["LC_ALL"] = candidate
        env["LANG"] = candidate
        proc = subprocess.run(
            [bash_bin, "-c", 'p="é"; printf "%s" "${#p}"'],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        if proc.returncode == 0 and proc.stdout.strip() == "1":
            return candidate
    return "en_US.UTF-8"


BASH_BIN = select_bash_bin()
UTF8_LOCALE = detect_utf8_locale(BASH_BIN)


def encode_expected_ascii(path_str: str) -> str:
    return "".join(ch if ch.isascii() and ch.isalnum() else "-" for ch in path_str)


class ClaudeRepoMemorySetupTest(unittest.TestCase):
    bash_version: str = ""

    @classmethod
    def setUpClass(cls) -> None:
        proc = subprocess.run(
            [BASH_BIN, "-c", 'printf "%s" "$BASH_VERSION"'],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=True,
        )
        cls.bash_version = proc.stdout.strip()
        if not cls.bash_version:
            raise AssertionError(f"Failed to read BASH_VERSION from {BASH_BIN}")

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(prefix="crm-setup-test-")
        self.root = Path(os.path.realpath(self._td.name))
        self.home = self.root / "home"
        self.tmp = self.root / "tmp"
        self.home.mkdir()
        self.tmp.mkdir()
        self.claude_dir = self.home / ".claude"

    def tearDown(self) -> None:
        self._td.cleanup()

    def base_env(self, extra: Optional[Dict[str, Optional[str]]] = None) -> Dict[str, str]:
        env = dict(os.environ)
        for key in SCRUB_ENV_KEYS:
            env.pop(key, None)
        env["HOME"] = str(self.home)
        env["TMPDIR"] = str(self.tmp)
        env["GIT_CONFIG_NOSYSTEM"] = "1"
        env["GIT_CONFIG_GLOBAL"] = str(self.home / ".gitconfig")
        env["LC_ALL"] = UTF8_LOCALE
        env["LANG"] = UTF8_LOCALE
        if extra:
            for key, val in extra.items():
                if val is None:
                    env.pop(key, None)
                else:
                    env[key] = val
        return env

    def run_script(
        self,
        args: List[str],
        cwd: Optional[Path] = None,
        env_extra: Optional[Dict[str, Optional[str]]] = None,
    ) -> subprocess.CompletedProcess:
        return subprocess.run(
            [BASH_BIN, str(SCRIPT_PATH), *args],
            cwd=str(cwd or self.root),
            env=self.base_env(env_extra),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    def git(
        self,
        repo: Path,
        args: List[str],
        env_extra: Optional[Dict[str, Optional[str]]] = None,
        check: bool = True,
    ) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", *args],
            cwd=str(repo),
            env=self.base_env(env_extra),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=check,
        )

    def init_git_repo(self, path: Path, with_initial_commit: bool = True) -> Path:
        path.mkdir(parents=True, exist_ok=True)
        self.git(path, ["init"])
        self.git(path, ["config", "user.name", "Test User"])
        self.git(path, ["config", "user.email", "test@example.com"])
        if with_initial_commit:
            (path / "README.md").write_text("# demo\n", encoding="utf-8")
            self.git(path, ["add", "README.md"])
            self.git(path, ["commit", "-m", "initial"])
        return path

    def global_project_dir(self, repo: Path, claude_base: Optional[Path] = None) -> Path:
        base = claude_base or self.claude_dir
        encoded = encode_expected_ascii(os.path.realpath(str(repo)))
        return base / "projects" / encoded

    def make_posix_bin_without_git(self) -> Path:
        bin_dir = self.root / "posix-bin-no-git"
        bin_dir.mkdir(exist_ok=True)
        tools = (
            "bash",
            "basename",
            "cat",
            "cp",
            "diff",
            "dirname",
            "find",
            "grep",
            "head",
            "ln",
            "ls",
            "mkdir",
            "mktemp",
            "mv",
            "readlink",
            "rm",
            "rmdir",
            "sed",
            "sort",
            "tr",
        )
        for tool in tools:
            found = shutil.which(tool)
            if found:
                dst = bin_dir / tool
                if not dst.exists():
                    os.symlink(found, dst)
        return bin_dir

    # ------------------------------------------------------------------
    # Group 1: CLI, path resolution, encoding & environment contracts (10)
    # ------------------------------------------------------------------

    def test_help_and_unknown_options_and_extra_args(self) -> None:
        self.assertTrue(os.access(SCRIPT_PATH, os.X_OK), "script must be executable")
        self.assertTrue(self.bash_version)

        for flag in ("-h", "--help"):
            res = self.run_script([flag])
            self.assertEqual(res.returncode, 0, res.stderr)
            self.assertIn("claude-repo-memory-setup.sh", res.stdout)

        res_unknown = self.run_script(["--nope"])
        self.assertEqual(res_unknown.returncode, 1)
        self.assertIn("Unknown option", res_unknown.stderr)

        res_extra = self.run_script([str(self.root), str(self.home)])
        self.assertEqual(res_extra.returncode, 1)
        self.assertIn("Too many arguments", res_extra.stderr)

    def test_missing_or_file_target(self) -> None:
        missing = self.root / "does-not-exist"
        res_missing = self.run_script([str(missing)])
        self.assertEqual(res_missing.returncode, 1)
        self.assertIn("does not exist", res_missing.stderr)

        regular_file = self.root / "plain.txt"
        regular_file.write_text("hi\n", encoding="utf-8")
        res_file = self.run_script([str(regular_file)])
        self.assertEqual(res_file.returncode, 1)
        self.assertIn("not a directory", res_file.stderr)

    def test_encode_ascii_and_utf8_bmp_and_raw_symlink(self) -> None:
        res_ascii = self.run_script(["--encode", "/a.b_c/My Repo--Test"])
        self.assertEqual(res_ascii.returncode, 0, res_ascii.stderr)
        self.assertEqual(res_ascii.stdout.strip(), "-a-b-c-My-Repo--Test")

        res_utf8 = self.run_script(["--encode", "/tmp/café/中文目录"])
        self.assertEqual(res_utf8.returncode, 0, res_utf8.stderr)
        self.assertEqual(res_utf8.stdout.strip(), "-tmp-caf------")

        # --encode must encode the raw path string without resolving symlinks
        real_dir = self.root / "real_target"
        real_dir.mkdir()
        link_dir = self.root / "alias_link"
        os.symlink(real_dir, link_dir)
        res_link = self.run_script(["--encode", str(link_dir)])
        self.assertEqual(res_link.returncode, 0, res_link.stderr)
        self.assertEqual(res_link.stdout.strip(), encode_expected_ascii(str(link_dir)))
        self.assertNotEqual(res_link.stdout.strip(), encode_expected_ascii(str(real_dir)))

    def test_encode_rejects_non_ascii_in_c_locale_and_4byte_emoji(self) -> None:
        c_env = {"LC_ALL": "C", "LANG": "C"}

        res_c_ascii = self.run_script(["--encode", "/tmp/Ascii_123.dir"], env_extra=c_env)
        self.assertEqual(res_c_ascii.returncode, 0, res_c_ascii.stderr)
        self.assertEqual(res_c_ascii.stdout.strip(), "-tmp-Ascii-123-dir")

        for bad_path in ("/tmp/café", "/tmp/中文"):
            res_c_non_ascii = self.run_script(["--encode", bad_path], env_extra=c_env)
            self.assertEqual(res_c_non_ascii.returncode, 1)
            self.assertEqual(res_c_non_ascii.stdout, "")
            self.assertIn("locale", res_c_non_ascii.stderr.lower())

        res_emoji = self.run_script(["--encode", "/tmp/proj-😀-dir"])
        self.assertEqual(res_emoji.returncode, 1)
        self.assertEqual(res_emoji.stdout, "")
        self.assertIn("4-byte UTF-8", res_emoji.stderr)

    def test_encode_boundary_200_vs_201(self) -> None:
        exact_200 = "/" + "a" * 199
        over_201 = "/" + "a" * 200

        res_200 = self.run_script(["--encode", exact_200])
        self.assertEqual(res_200.returncode, 0, res_200.stderr)
        self.assertEqual(len(res_200.stdout.strip()), 200)

        res_201 = self.run_script(["--encode", over_201])
        self.assertEqual(res_201.returncode, 1)
        self.assertEqual(res_201.stdout, "")
        self.assertIn("200", res_201.stderr)

    def test_dash_prefixed_and_bare_dash_dir_and_cdpath(self) -> None:
        weird_repo = self.init_git_repo(self.root / "-weird-repo")
        res_weird = self.run_script(["--", "-weird-repo"], cwd=self.root)
        self.assertEqual(res_weird.returncode, 0, res_weird.stderr)
        self.assertTrue((self.global_project_dir(weird_repo) / "memory").is_symlink())

        bare_dash = self.init_git_repo(self.root / "-")
        res_dash = self.run_script(["--", "-"], cwd=self.root)
        self.assertEqual(res_dash.returncode, 0, res_dash.stderr)
        gmem = self.global_project_dir(bare_dash) / "memory"
        self.assertTrue(gmem.is_symlink())
        self.assertEqual(os.path.realpath(str(gmem)), str(bare_dash / ".agents" / "memory"))

        # CDPATH hijack defense: create a decoy dir under a CDPATH entry with the same name
        cdpath_base = self.root / "cdpath_base"
        (cdpath_base / "proj").mkdir(parents=True)
        work_base = self.root / "work_base"
        real_proj = self.init_git_repo(work_base / "proj")
        res_cdpath = self.run_script(
            ["proj"],
            cwd=work_base,
            env_extra={"CDPATH": f"{cdpath_base}:/"},
        )
        self.assertEqual(res_cdpath.returncode, 0, res_cdpath.stderr)
        self.assertTrue((real_proj / ".agents" / "memory").is_dir())
        self.assertFalse((cdpath_base / "proj" / ".agents").exists())

    def test_env_precedence_claude_home_vs_config_dir(self) -> None:
        repo1 = self.init_git_repo(self.root / "env_default")
        res1 = self.run_script([str(repo1)])
        self.assertEqual(res1.returncode, 0, res1.stderr)
        self.assertTrue((self.global_project_dir(repo1, self.claude_dir) / "memory").is_symlink())

        cfg_dir = self.root / "custom_cfg"
        repo2 = self.init_git_repo(self.root / "env_cfg")
        res2 = self.run_script(
            [str(repo2)],
            env_extra={"CLAUDE_CONFIG_DIR": f"{cfg_dir}///"},
        )
        self.assertEqual(res2.returncode, 0, res2.stderr)
        self.assertTrue((self.global_project_dir(repo2, cfg_dir) / "memory").is_symlink())
        self.assertNotIn("//projects", res2.stdout)

        home_override = self.root / "override_home"
        repo3 = self.init_git_repo(self.root / "env_precedence")
        res3 = self.run_script(
            [str(repo3)],
            env_extra={
                "CLAUDE_CONFIG_DIR": str(cfg_dir),
                "CLAUDE_HOME": f"{home_override}/",
            },
        )
        self.assertEqual(res3.returncode, 0, res3.stderr)
        self.assertTrue((self.global_project_dir(repo3, home_override) / "memory").is_symlink())
        self.assertFalse((self.global_project_dir(repo3, cfg_dir) / "memory").exists())

    def test_claude_code_project_dir_name_refused(self) -> None:
        repo = self.init_git_repo(self.root / "override_key_repo")
        for args in ([str(repo)], ["--check-only", str(repo)]):
            res = self.run_script(
                args,
                env_extra={"CLAUDE_CODE_PROJECT_DIR_NAME": "custom-key"},
            )
            self.assertEqual(res.returncode, 1)
            self.assertIn("CLAUDE_CODE_PROJECT_DIR_NAME", res.stderr)
            self.assertFalse((repo / ".agents").exists())
            self.assertFalse(self.claude_dir.exists())

    def test_check_only_preflight_is_read_only(self) -> None:
        repo = self.init_git_repo(self.root / "preflight_repo")
        res_ok = self.run_script(["--check-only", str(repo)])
        self.assertEqual(res_ok.returncode, 0, res_ok.stderr)
        self.assertIn("Preflight OK", res_ok.stdout)
        self.assertFalse((repo / ".agents").exists())
        self.assertFalse(self.claude_dir.exists())

        sub = repo / "sub"
        sub.mkdir()
        res_bad = self.run_script(["--check-only", str(sub)])
        self.assertEqual(res_bad.returncode, 1)
        self.assertFalse((sub / ".agents").exists())
        self.assertFalse((repo / ".agents").exists())
        self.assertFalse(self.claude_dir.exists())

    def _make_path_with_exact_length(self, target_len: int, prefix_tag: str) -> Path:
        base = self.root / prefix_tag
        base.mkdir(parents=True, exist_ok=True)
        cur = Path(os.path.realpath(str(base)))
        remaining = target_len - len(str(cur))
        self.assertGreater(remaining, 2, f"Temp root already too long ({len(str(cur))}) for {target_len}")
        while remaining > 60:
            seg = "d" * 50
            cur = cur / seg
            remaining -= len(seg) + 1
        cur = cur / ("x" * (remaining - 1))
        cur.mkdir(parents=True, exist_ok=True)
        self.assertEqual(len(os.path.realpath(str(cur))), target_len)
        return cur

    def test_main_over_200_chars_refused_and_exact_200_allowed(self) -> None:
        repo_200 = self._make_path_with_exact_length(200, "len200")
        self.init_git_repo(repo_200)
        res_200 = self.run_script([str(repo_200)])
        self.assertEqual(res_200.returncode, 0, res_200.stderr)
        self.assertTrue((self.global_project_dir(repo_200) / "memory").is_symlink())

        repo_201 = self._make_path_with_exact_length(201, "len201")
        self.init_git_repo(repo_201)
        res_201 = self.run_script([str(repo_201)])
        self.assertEqual(res_201.returncode, 1)
        self.assertIn("200", res_201.stderr)
        self.assertFalse((repo_201 / ".agents").exists())

    # ------------------------------------------------------------------
    # Group 2: Main flow, idempotency, multi-machine & data safety (11)
    # ------------------------------------------------------------------

    def test_fresh_git_repo_no_existing_memory(self) -> None:
        repo = self.init_git_repo(self.root / "fresh_git")
        before_head = self.git(repo, ["rev-parse", "HEAD"]).stdout.strip()

        res = self.run_script([str(repo)])
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("Symlink write probe passed", res.stdout)
        self.assertIn("warnings: none", res.stdout)

        gmem = self.global_project_dir(repo) / "memory"
        rmem = repo / ".agents" / "memory"
        self.assertTrue(gmem.is_symlink())
        self.assertTrue(rmem.is_dir())
        self.assertEqual(os.path.realpath(str(gmem)), str(rmem))
        # No probe residue
        self.assertEqual(list(rmem.iterdir()), [])
        # Empty commit skipped
        after_head = self.git(repo, ["rev-parse", "HEAD"]).stdout.strip()
        self.assertEqual(before_head, after_head)

    def test_migrate_existing_memory_and_isolated_commit(self) -> None:
        repo = self.init_git_repo(self.root / "migrate_repo")
        # Create unrelated staged & unstaged changes in repo
        (repo / "staged.txt").write_text("staged-content\n", encoding="utf-8")
        self.git(repo, ["add", "staged.txt"])
        (repo / "unstaged.txt").write_text("unstaged-content\n", encoding="utf-8")

        gproj = self.global_project_dir(repo)
        gmem = gproj / "memory"
        (gmem / "topics").mkdir(parents=True)
        (gmem / "MEMORY.md").write_text("# Index\n- topics/arch.md\n", encoding="utf-8")
        (gmem / "topics" / "arch.md").write_text("Architecture note\n", encoding="utf-8")
        (gmem / ".hidden_note").write_text("dotfile\n", encoding="utf-8")
        # Sibling files outside memory/ must stay untouched
        sibling = gproj / "sessions.jsonl"
        sibling.write_text('{"id": 1}\n', encoding="utf-8")

        res = self.run_script([str(repo)])
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertTrue(gmem.is_symlink())
        self.assertEqual(sibling.read_text(encoding="utf-8"), '{"id": 1}\n')
        self.assertFalse((gproj / "memory.bak").exists())

        rmem = repo / ".agents" / "memory"
        self.assertEqual((rmem / "MEMORY.md").read_text(encoding="utf-8"), "# Index\n- topics/arch.md\n")
        self.assertEqual((rmem / "topics" / "arch.md").read_text(encoding="utf-8"), "Architecture note\n")
        self.assertEqual((rmem / ".hidden_note").read_text(encoding="utf-8"), "dotfile\n")

        # Verify commit message and pathspec isolation
        commit_files = self.git(repo, ["show", "--name-only", "--pretty=format:", "HEAD"]).stdout.splitlines()
        commit_files = [f for f in commit_files if f.strip()]
        self.assertTrue(commit_files)
        for f in commit_files:
            self.assertTrue(f.startswith(".agents/memory/"), f"Unexpected file in commit: {f}")

        status = self.git(repo, ["status", "--porcelain"]).stdout
        self.assertIn("A  staged.txt", status)
        self.assertIn("?? unstaged.txt", status)

    def test_unborn_head_git_repo_migration(self) -> None:
        repo = self.init_git_repo(self.root / "unborn_repo", with_initial_commit=False)
        (repo / "other.txt").write_text("stay staged\n", encoding="utf-8")
        self.git(repo, ["add", "other.txt"])

        gmem = self.global_project_dir(repo) / "memory"
        gmem.mkdir(parents=True)
        (gmem / "MEMORY.md").write_text("root memory\n", encoding="utf-8")

        res = self.run_script([str(repo)])
        self.assertEqual(res.returncode, 0, res.stderr)

        commit_files = self.git(repo, ["show", "--name-only", "--pretty=format:", "HEAD"]).stdout.splitlines()
        commit_files = [f for f in commit_files if f.strip()]
        self.assertEqual(commit_files, [".agents/memory/MEMORY.md"])
        status = self.git(repo, ["status", "--porcelain"]).stdout
        self.assertIn("A  other.txt", status)

    def test_idempotent_rerun_preserves_preexisting_backups(self) -> None:
        repo = self.init_git_repo(self.root / "idempotent_repo")
        gproj = self.global_project_dir(repo)
        gmem = gproj / "memory"
        gmem.mkdir(parents=True)
        (gmem / "MEMORY.md").write_text("v1\n", encoding="utf-8")

        res1 = self.run_script([str(repo)])
        self.assertEqual(res1.returncode, 0, res1.stderr)
        head1 = self.git(repo, ["rev-parse", "HEAD"]).stdout.strip()

        # Plant a pre-existing manual/failed backup with unique content
        old_bak = gproj / "memory.bak"
        old_bak.mkdir()
        (old_bak / "precious.md").write_text("do not delete me\n", encoding="utf-8")

        res2 = self.run_script([str(repo)])
        self.assertEqual(res2.returncode, 0, res2.stderr)
        head2 = self.git(repo, ["rev-parse", "HEAD"]).stdout.strip()
        self.assertEqual(head1, head2)
        self.assertTrue((old_bak / "precious.md").exists())
        self.assertEqual((old_bak / "precious.md").read_text(encoding="utf-8"), "do not delete me\n")

    def test_preexisting_bak_survives_new_migration_bak2_cleaned(self) -> None:
        repo = self.init_git_repo(self.root / "preexisting_bak_repo")
        gproj = self.global_project_dir(repo)
        old_bak = gproj / "memory.bak"
        old_bak.mkdir(parents=True)
        (old_bak / "legacy.md").write_text("keep forever\n", encoding="utf-8")

        gmem = gproj / "memory"
        gmem.mkdir(parents=True)
        (gmem / "MEMORY.md").write_text("fresh migration\n", encoding="utf-8")

        res = self.run_script([str(repo)])
        self.assertEqual(res.returncode, 0, res.stderr)
        # This run used memory.bak2 and cleaned it; old memory.bak stays intact
        self.assertFalse((gproj / "memory.bak2").exists())
        self.assertTrue((old_bak / "legacy.md").exists())
        self.assertEqual((old_bak / "legacy.md").read_text(encoding="utf-8"), "keep forever\n")
        self.assertTrue((gproj / "memory").is_symlink())

    def test_multimachine_clone_with_empty_global_dir_uses_rmdir(self) -> None:
        repo = self.init_git_repo(self.root / "multimachine_repo")
        rmem = repo / ".agents" / "memory"
        rmem.mkdir(parents=True)
        (rmem / "MEMORY.md").write_text("shared from machine A\n", encoding="utf-8")
        self.git(repo, ["add", ".agents/memory"])
        self.git(repo, ["commit", "-m", "add shared memory"])

        # Machine B: Claude created an empty global memory/ dir before running setup
        gproj = self.global_project_dir(repo)
        gmem = gproj / "memory"
        gmem.mkdir(parents=True)

        res = self.run_script([str(repo)])
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("rmdir", res.stdout)
        self.assertTrue(gmem.is_symlink())
        self.assertFalse((gproj / "memory.bak").exists())
        self.assertEqual((gmem / "MEMORY.md").read_text(encoding="utf-8"), "shared from machine A\n")

    def test_unreadable_global_memory_dir_fails_not_treated_as_empty(self) -> None:
        if os.geteuid() == 0:
            self.skipTest("chmod 000 is still readable by root")

        repo = self.init_git_repo(self.root / "unreadable_repo")
        gmem = self.global_project_dir(repo) / "memory"
        gmem.mkdir(parents=True)
        (gmem / "secret.md").write_text("important\n", encoding="utf-8")

        os.chmod(gmem, 0o000)
        try:
            res = self.run_script([str(repo)])
            self.assertEqual(res.returncode, 1)
            self.assertIn("Cannot read directory", res.stderr)
            self.assertFalse(gmem.is_symlink())
            self.assertFalse((self.global_project_dir(repo) / "memory.bak").exists())
        finally:
            os.chmod(gmem, 0o700)

        self.assertEqual((gmem / "secret.md").read_text(encoding="utf-8"), "important\n")

    def test_both_nonempty_identical_vs_different(self) -> None:
        # Case A: identical content -> symlink created, backup cleaned
        repo_same = self.init_git_repo(self.root / "both_same")
        (repo_same / ".agents" / "memory").mkdir(parents=True)
        (repo_same / ".agents" / "memory" / "MEMORY.md").write_text("same\n", encoding="utf-8")
        gmem_same = self.global_project_dir(repo_same) / "memory"
        gmem_same.mkdir(parents=True)
        (gmem_same / "MEMORY.md").write_text("same\n", encoding="utf-8")

        res_same = self.run_script([str(repo_same)])
        self.assertEqual(res_same.returncode, 0, res_same.stderr)
        self.assertTrue(gmem_same.is_symlink())
        self.assertFalse((self.global_project_dir(repo_same) / "memory.bak").exists())

        # Case B: different content -> refused, zero overwrite, no body leaked to stderr
        repo_diff = self.init_git_repo(self.root / "both_diff")
        (repo_diff / ".agents" / "memory").mkdir(parents=True)
        (repo_diff / ".agents" / "memory" / "MEMORY.md").write_text("SUPER_PRIVATE_REPO_TOKEN\n", encoding="utf-8")
        gmem_diff = self.global_project_dir(repo_diff) / "memory"
        gmem_diff.mkdir(parents=True)
        (gmem_diff / "MEMORY.md").write_text("SUPER_PRIVATE_GLOBAL_TOKEN\n", encoding="utf-8")

        res_diff = self.run_script([str(repo_diff)])
        self.assertEqual(res_diff.returncode, 1)
        self.assertNotIn("SUPER_PRIVATE_REPO_TOKEN", res_diff.stderr)
        self.assertNotIn("SUPER_PRIVATE_GLOBAL_TOKEN", res_diff.stderr)
        self.assertEqual(
            (repo_diff / ".agents" / "memory" / "MEMORY.md").read_text(encoding="utf-8"),
            "SUPER_PRIVATE_REPO_TOKEN\n",
        )
        self.assertEqual(
            (gmem_diff / "MEMORY.md").read_text(encoding="utf-8"),
            "SUPER_PRIVATE_GLOBAL_TOKEN\n",
        )
        self.assertFalse(gmem_diff.is_symlink())

    def test_dangling_symlink_repaired_and_collision_refused_and_nondir_refused(self) -> None:
        # 1. Dangling symlink repaired
        repo_dangle = self.init_git_repo(self.root / "dangle_repo")
        gproj_dangle = self.global_project_dir(repo_dangle)
        gproj_dangle.mkdir(parents=True)
        os.symlink(self.root / "nonexistent_target", gproj_dangle / "memory")

        res_dangle = self.run_script([str(repo_dangle)])
        self.assertEqual(res_dangle.returncode, 0, res_dangle.stderr)
        self.assertEqual(
            os.path.realpath(str(gproj_dangle / "memory")),
            str(repo_dangle / ".agents" / "memory"),
        )

        # 2. Collision: symlink pointing to another existing directory refused
        repo_col = self.init_git_repo(self.root / "collision_repo")
        other_dir = self.root / "other_real_memory"
        other_dir.mkdir()
        gproj_col = self.global_project_dir(repo_col)
        gproj_col.mkdir(parents=True)
        os.symlink(other_dir, gproj_col / "memory")

        res_col = self.run_script([str(repo_col)])
        self.assertEqual(res_col.returncode, 1)
        self.assertIn("WARN:", res_col.stderr)
        self.assertEqual(os.readlink(str(gproj_col / "memory")), str(other_dir))

        # 3. Non-dir regular file occupying global memory refused
        repo_file = self.init_git_repo(self.root / "nondir_repo")
        gproj_file = self.global_project_dir(repo_file)
        gproj_file.mkdir(parents=True)
        (gproj_file / "memory").write_text("not a dir\n", encoding="utf-8")

        res_file = self.run_script([str(repo_file)])
        self.assertEqual(res_file.returncode, 1)
        self.assertIn("neither a directory nor a symlink", res_file.stderr)
        self.assertEqual((gproj_file / "memory").read_text(encoding="utf-8"), "not a dir\n")

    def test_repo_agents_symlink_escape_and_overlap_refused(self) -> None:
        outside = self.root / "outside_dir"
        outside.mkdir()

        # .agents is symlink escaping repo
        repo1 = self.init_git_repo(self.root / "escape_agents")
        os.symlink(outside, repo1 / ".agents")
        res1 = self.run_script([str(repo1)])
        self.assertEqual(res1.returncode, 1)
        self.assertIn(".agents must be a real directory", res1.stderr)
        self.assertFalse(self.global_project_dir(repo1).exists())

        # .agents/memory is symlink escaping repo
        repo2 = self.init_git_repo(self.root / "escape_memory")
        (repo2 / ".agents").mkdir()
        os.symlink(outside, repo2 / ".agents" / "memory")
        res2 = self.run_script([str(repo2)])
        self.assertEqual(res2.returncode, 1)
        self.assertIn(".agents/memory must be a real directory", res2.stderr)

        # CLAUDE_CONFIG_DIR inside repo causes overlap
        repo3 = self.init_git_repo(self.root / "overlap_repo")
        res3 = self.run_script(
            [str(repo3)],
            env_extra={"CLAUDE_CONFIG_DIR": str(repo3 / ".agents" / "memory")},
        )
        self.assertEqual(res3.returncode, 1)
        self.assertIn("overlaps", res3.stderr)
        self.assertFalse((repo3 / ".agents").exists())

    def test_symlink_inside_memory_dir_refused(self) -> None:
        repo = self.init_git_repo(self.root / "inner_symlink_repo")
        gproj = self.global_project_dir(repo)
        gmem = gproj / "memory"
        gmem.mkdir(parents=True)
        (gmem / "MEMORY.md").write_text("ok\n", encoding="utf-8")
        os.symlink(self.root / "dangling-target", gmem / "bad-link")

        res = self.run_script([str(repo)])
        self.assertEqual(res.returncode, 1)
        self.assertIn("symlinks or special files", res.stderr)
        self.assertFalse(gmem.is_symlink())
        self.assertTrue((gmem / "MEMORY.md").exists())
        self.assertFalse((gproj / "memory.bak").exists())

    def test_symlink_inside_repo_memory_refused_on_fresh_clone_and_rerun(self) -> None:
        outside_file = self.root / "outside_secret.md"
        outside_file.write_text("secret\n", encoding="utf-8")

        # 1. Fresh clone (no global memory dir yet) with symlink inside .agents/memory/
        repo1 = self.init_git_repo(self.root / "fresh_clone_repo_symlink")
        rmem1 = repo1 / ".agents" / "memory"
        rmem1.mkdir(parents=True)
        os.symlink(outside_file, rmem1 / "MEMORY.md")

        for args in (["--check-only", str(repo1)], [str(repo1)]):
            res = self.run_script(args)
            self.assertEqual(res.returncode, 1)
            self.assertIn("symlinks or special files", res.stderr)
            self.assertFalse(self.global_project_dir(repo1).exists())

        # 2. Idempotent rerun after symlink planted inside .agents/memory/
        repo2 = self.init_git_repo(self.root / "rerun_repo_symlink")
        res_init = self.run_script([str(repo2)])
        self.assertEqual(res_init.returncode, 0, res_init.stderr)
        os.symlink(outside_file, repo2 / ".agents" / "memory" / "escape.md")

        res_rerun = self.run_script([str(repo2)])
        self.assertEqual(res_rerun.returncode, 1)
        self.assertIn("symlinks or special files", res_rerun.stderr)

    def test_newline_only_filename_in_repo_memory_not_overwritten(self) -> None:
        repo = self.init_git_repo(self.root / "newline_file_repo")
        rmem = repo / ".agents" / "memory"
        rmem.mkdir(parents=True)
        nl_name = "\n"
        (rmem / nl_name).write_text("precious repo content\n", encoding="utf-8")

        gproj = self.global_project_dir(repo)
        gmem = gproj / "memory"
        gmem.mkdir(parents=True)
        (gmem / nl_name).write_text("different global content\n", encoding="utf-8")
        (gmem / "MEMORY.md").write_text("global index\n", encoding="utf-8")

        res = self.run_script([str(repo)])
        self.assertEqual(res.returncode, 1)
        self.assertIn("differ", res.stderr)
        self.assertEqual((rmem / nl_name).read_text(encoding="utf-8"), "precious repo content\n")
        self.assertFalse(gmem.is_symlink())

    def test_trailing_newline_project_dir_refused_without_touching_sibling(self) -> None:
        sibling = self.init_git_repo(self.root / "demo")
        nl_dir = self.root / "demo\n"
        nl_dir.mkdir()
        link_to_nl = self.root / "link_to_nl"
        os.symlink(nl_dir, link_to_nl)

        for target in (nl_dir, link_to_nl):
            res = self.run_script([str(target)])
            self.assertEqual(res.returncode, 1)
            self.assertIn("newline", res.stderr.lower())
            self.assertFalse((sibling / ".agents").exists())
            self.assertFalse(self.global_project_dir(sibling).exists())

    def test_dotdot_nonexistent_and_case_insensitive_overlap_and_git_root(self) -> None:
        repo = self.init_git_repo(self.root / "CaseRepo")

        # Non-existent segment followed by '..' in CLAUDE_HOME must be refused
        dotdot_home = str(repo / "nonexistent_dir" / ".." / ".agents" / "memory")
        for args in (["--check-only", str(repo)], [str(repo)]):
            res = self.run_script(args, env_extra={"CLAUDE_HOME": dotdot_home})
            self.assertEqual(res.returncode, 1)
            self.assertIn("..", res.stderr)
            self.assertFalse((repo / ".agents").exists())

        # Case-insensitive filesystem checks (e.g. macOS APFS default)
        flipped_repo = self.root / "caserepo"
        if flipped_repo.exists() and os.path.samefile(repo, flipped_repo):
            # 1. CLAUDE_HOME overlapping repo with different case must be refused
            flipped_home = str(flipped_repo / ".agents" / "memory")
            for args in (["--check-only", str(repo)], [str(repo)]):
                res_ov = self.run_script(args, env_extra={"CLAUDE_HOME": flipped_home})
                self.assertEqual(res_ov.returncode, 1)
                self.assertIn("overlaps", res_ov.stderr)
                self.assertFalse((repo / ".agents").exists())

            # 2. Passing a legitimate Git main root with different case must succeed
            #    and canonicalize to the on-disk Git toplevel path
            res_case_check = self.run_script(["--check-only", str(flipped_repo)])
            self.assertEqual(res_case_check.returncode, 0, res_case_check.stderr)
            self.assertIn(f"Preflight OK: {repo}", res_case_check.stdout)

            res_case_run = self.run_script([str(flipped_repo)])
            self.assertEqual(res_case_run.returncode, 0, res_case_run.stderr)
            self.assertTrue((self.global_project_dir(repo) / "memory").is_symlink())

    def test_ln_failure_rolls_back_backup_to_global_memory(self) -> None:
        repo = self.init_git_repo(self.root / "ln_fail_repo")
        gproj = self.global_project_dir(repo)
        gmem = gproj / "memory"
        gmem.mkdir(parents=True)
        (gmem / "MEMORY.md").write_text("must survive ln failure\n", encoding="utf-8")

        shim_dir = self.root / "fail_ln_bin"
        shim_dir.mkdir()
        fake_ln = shim_dir / "ln"
        fake_ln.write_text("#!/usr/bin/env bash\necho 'simulated ln failure' >&2\nexit 1\n", encoding="utf-8")
        fake_ln.chmod(0o755)

        res = self.run_script(
            [str(repo)],
            env_extra={"PATH": f"{shim_dir}:{os.environ.get('PATH', '')}"},
        )
        self.assertEqual(res.returncode, 1)
        self.assertIn("Restored original global memory from backup", res.stderr)
        self.assertTrue(gmem.is_dir() and not gmem.is_symlink())
        self.assertEqual((gmem / "MEMORY.md").read_text(encoding="utf-8"), "must survive ln failure\n")
        self.assertFalse((gproj / "memory.bak").exists())

    def test_rerun_preserves_preexisting_uncommitted_memory_changes(self) -> None:
        repo = self.init_git_repo(self.root / "dirty_rerun_repo")
        gproj = self.global_project_dir(repo)
        gmem = gproj / "memory"
        gmem.mkdir(parents=True)
        (gmem / "MEMORY.md").write_text("v1\n", encoding="utf-8")

        res1 = self.run_script([str(repo)])
        self.assertEqual(res1.returncode, 0, res1.stderr)
        head_after_init = self.git(repo, ["rev-parse", "HEAD"]).stdout.strip()

        # Make partially staged + unstaged edits inside .agents/memory/
        rmem = repo / ".agents" / "memory"
        (rmem / "MEMORY.md").write_text("v1\nstaged line\n", encoding="utf-8")
        self.git(repo, ["add", ".agents/memory/MEMORY.md"])
        (rmem / "MEMORY.md").write_text("v1\nstaged line\nunstaged line\n", encoding="utf-8")
        (rmem / "draft.md").write_text("untracked draft\n", encoding="utf-8")

        res2 = self.run_script([str(repo)])
        self.assertEqual(res2.returncode, 0, res2.stderr)
        self.assertIn("WARN:", res2.stderr)
        self.assertIn("Pre-existing uncommitted changes", res2.stderr)

        # HEAD, staged diff, working tree diff, and untracked file must all stay untouched
        self.assertEqual(self.git(repo, ["rev-parse", "HEAD"]).stdout.strip(), head_after_init)
        status = self.git(repo, ["status", "--porcelain", "--", ".agents/memory/"]).stdout
        self.assertIn("MM .agents/memory/MEMORY.md", status)
        self.assertIn("?? .agents/memory/draft.md", status)
        cached_diff = self.git(repo, ["diff", "--cached", "--", ".agents/memory/MEMORY.md"]).stdout
        self.assertIn("+staged line", cached_diff)
        self.assertNotIn("+unstaged line", cached_diff)

    # ------------------------------------------------------------------
    # Group 3: VCS isolation, .gitignore blackhole & commit failure (4)
    # ------------------------------------------------------------------

    def test_gitignore_directory_blocked_preserves_backup(self) -> None:
        repo = self.init_git_repo(self.root / "ignored_dir_repo")
        (repo / ".gitignore").write_text(".agents/memory/\n", encoding="utf-8")
        self.git(repo, ["add", ".gitignore"])
        self.git(repo, ["commit", "-m", "ignore memory dir"])

        gproj = self.global_project_dir(repo)
        gmem = gproj / "memory"
        gmem.mkdir(parents=True)
        (gmem / "MEMORY.md").write_text("important\n", encoding="utf-8")

        res = self.run_script([str(repo)])
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("WARN:", res.stderr)
        self.assertTrue((gproj / "memory.bak").is_dir())
        self.assertTrue(gmem.is_symlink())

    def test_gitignore_markdown_blackhole_fails_and_preserves_backup(self) -> None:
        repo = self.init_git_repo(self.root / "md_blackhole_repo")
        (repo / ".gitignore").write_text("*.md\n", encoding="utf-8")
        self.git(repo, ["add", ".gitignore"])
        self.git(repo, ["commit", "-m", "ignore all md"])

        gproj = self.global_project_dir(repo)
        gmem = gproj / "memory"
        gmem.mkdir(parents=True)
        (gmem / "MEMORY.md").write_text("would be lost on git clean\n", encoding="utf-8")

        res = self.run_script([str(repo)])
        self.assertEqual(res.returncode, 1)
        self.assertIn("0 regular files are tracked", res.stderr)
        self.assertTrue((gproj / "memory.bak").is_dir())
        self.assertTrue((gproj / "memory.bak" / "MEMORY.md").exists())

    def test_partial_gitignore_with_tracked_files_succeeds(self) -> None:
        repo = self.init_git_repo(self.root / "partial_ignore_repo")
        (repo / ".gitignore").write_text("*.tmp\n", encoding="utf-8")
        self.git(repo, ["add", ".gitignore"])
        self.git(repo, ["commit", "-m", "ignore tmp only"])

        gproj = self.global_project_dir(repo)
        gmem = gproj / "memory"
        gmem.mkdir(parents=True)
        (gmem / "MEMORY.md").write_text("tracked\n", encoding="utf-8")
        (gmem / "scratch.tmp").write_text("ignored\n", encoding="utf-8")

        res = self.run_script([str(repo)])
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertFalse((gproj / "memory.bak").exists())
        tracked = self.git(repo, ["ls-files", ".agents/memory/"]).stdout.splitlines()
        self.assertEqual(tracked, [".agents/memory/MEMORY.md"])

    def test_git_commit_failure_preserves_backup_and_link(self) -> None:
        repo = self.init_git_repo(self.root / "commit_fail_repo")
        # Force deterministic commit failure regardless of host user/hostname
        self.git(repo, ["config", "--unset", "user.name"])
        self.git(repo, ["config", "--unset", "user.email"])
        self.git(repo, ["config", "user.useConfigOnly", "true"])

        gproj = self.global_project_dir(repo)
        gmem = gproj / "memory"
        gmem.mkdir(parents=True)
        (gmem / "MEMORY.md").write_text("must stay safe\n", encoding="utf-8")

        res = self.run_script([str(repo)])
        self.assertEqual(res.returncode, 1)
        self.assertIn("git commit failed", res.stderr)
        self.assertTrue(gmem.is_symlink())
        self.assertTrue((gproj / "memory.bak" / "MEMORY.md").exists())
        self.assertEqual(
            (repo / ".agents" / "memory" / "MEMORY.md").read_text(encoding="utf-8"),
            "must stay safe\n",
        )

    # ------------------------------------------------------------------
    # Group 4: Git repository boundary enforcement (6)
    # ------------------------------------------------------------------

    def test_git_subdirectory_refused_with_root_hint(self) -> None:
        repo = self.init_git_repo(self.root / "sub_root")
        sub = repo / "packages" / "app"
        sub.mkdir(parents=True)

        res = self.run_script([str(sub)])
        self.assertEqual(res.returncode, 1)
        self.assertIn(str(repo), res.stderr)
        self.assertFalse((sub / ".agents").exists())
        self.assertFalse((repo / ".agents").exists())
        self.assertFalse(self.claude_dir.exists())

    def test_linked_worktree_root_and_subdir_refused_with_verified_main_root(self) -> None:
        repo = self.init_git_repo(self.root / "main_repo")
        wt = self.root / "linked_wt"
        self.git(repo, ["worktree", "add", "-b", "feat-wt", str(wt)])
        wt_sub = wt / "nested" / "dir"
        wt_sub.mkdir(parents=True)

        for target in (wt, wt_sub):
            res = self.run_script([str(target)])
            self.assertEqual(res.returncode, 1)
            self.assertIn("linked Git worktree", res.stderr)
            self.assertIn(str(repo), res.stderr)
            self.assertFalse((wt / ".agents").exists())
            self.assertFalse((repo / ".agents").exists())
            self.assertFalse(self.claude_dir.exists())

    def test_bare_repo_and_bare_worktree_and_dot_git_refused(self) -> None:
        # 1. Bare repository itself
        bare = self.root / "bare.git"
        bare.mkdir()
        self.git(bare, ["init", "--bare"])
        res_bare = self.run_script([str(bare)])
        self.assertEqual(res_bare.returncode, 1)
        self.assertIn("bare repository", res_bare.stderr)
        self.assertFalse((bare / ".agents").exists())

        # 2. Linked worktree derived from a bare repository
        seed = self.init_git_repo(self.root / "seed_for_bare")
        self.git(seed, ["push", str(bare), "HEAD:refs/heads/main"])
        bare_wt = self.root / "bare_wt"
        self.git(bare, ["worktree", "add", str(bare_wt), "main"])
        res_bare_wt = self.run_script([str(bare_wt)])
        self.assertEqual(res_bare_wt.returncode, 1)
        self.assertIn("autoMemoryDirectory", res_bare_wt.stderr)
        self.assertFalse((bare_wt / ".agents").exists())

        # 3. Inside .git metadata dir of a standard repo
        std = self.init_git_repo(self.root / "std_for_dotgit")
        res_dotgit = self.run_script([str(std / ".git")])
        self.assertEqual(res_dotgit.returncode, 1)
        self.assertIn(".git metadata", res_dotgit.stderr)
        self.assertFalse((std / ".git" / ".agents").exists())

    def test_git_submodule_and_separate_git_dir_refused_without_dirname_bug(self) -> None:
        # 1. Submodule (root, subdir, and linked worktree of submodule)
        upstream = self.init_git_repo(self.root / "sub_upstream")
        (upstream / "pkg").mkdir()
        (upstream / "pkg" / "mod.txt").write_text("hi\n", encoding="utf-8")
        self.git(upstream, ["add", "pkg/mod.txt"])
        self.git(upstream, ["commit", "-m", "add pkg"])

        super_repo = self.init_git_repo(self.root / "super_repo")
        self.git(
            super_repo,
            ["-c", "protocol.file.allow=always", "submodule", "add", str(upstream), "libs/sub"],
        )
        self.git(super_repo, ["commit", "-m", "add submodule"])

        sub_root = super_repo / "libs" / "sub"
        sub_child = sub_root / "pkg"
        sub_wt = self.root / "sub_wt"
        self.git(sub_root, ["worktree", "add", "-b", "sub-branch", str(sub_wt)])

        for target in (sub_root, sub_child):
            res = self.run_script([str(target)])
            self.assertEqual(res.returncode, 1)
            self.assertIn("submodule", res.stderr.lower())
            self.assertNotIn("re-run on the main root", res.stderr)
            self.assertFalse((target / ".agents").exists())

        res_sub_wt = self.run_script([str(sub_wt)])
        self.assertEqual(res_sub_wt.returncode, 1)
        self.assertNotIn("re-run on the main root", res_sub_wt.stderr)
        self.assertNotIn(f'"{super_repo / ".git" / "modules"}"', res_sub_wt.stderr)
        self.assertFalse((sub_wt / ".agents").exists())

        # 1b. Legacy submodule layout with a real .git/ directory (not gitfile)
        legacy_super = self.init_git_repo(self.root / "legacy_super")
        self.git(
            legacy_super,
            ["-c", "protocol.file.allow=always", "submodule", "add", str(upstream), "libs/legacy_sub"],
        )
        self.git(legacy_super, ["commit", "-m", "add legacy submodule"])
        legacy_sub = legacy_super / "libs" / "legacy_sub"
        (legacy_sub / ".git").unlink()
        shutil.move(str(legacy_super / ".git" / "modules" / "libs" / "legacy_sub"), str(legacy_sub / ".git"))
        self.git(
            legacy_super,
            ["config", "--file", str(legacy_sub / ".git" / "config"), "--unset", "core.worktree"],
        )
        self.assertTrue((legacy_sub / ".git").is_dir())
        for target in (legacy_sub, legacy_sub / "pkg"):
            res_leg = self.run_script([str(target)])
            self.assertEqual(res_leg.returncode, 1)
            self.assertIn("submodule", res_leg.stderr.lower())
            self.assertFalse((target / ".agents").exists())

        # 2. --separate-git-dir=/.../elsewhere/.git (root, subdir, and its worktree)
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        sep_git_dir = elsewhere / ".git"
        sep_repo = self.root / "sep_repo"
        sep_repo.mkdir()
        self.git(sep_repo, ["init", f"--separate-git-dir={sep_git_dir}"])
        self.git(sep_repo, ["config", "user.name", "Test User"])
        self.git(sep_repo, ["config", "user.email", "test@example.com"])
        (sep_repo / "sub").mkdir()
        (sep_repo / "sub" / "a.txt").write_text("a\n", encoding="utf-8")
        self.git(sep_repo, ["add", "sub/a.txt"])
        self.git(sep_repo, ["commit", "-m", "sep init"])

        sep_wt = self.root / "sep_wt"
        self.git(sep_repo, ["worktree", "add", "-b", "sep-branch", str(sep_wt)])

        for target in (sep_repo, sep_repo / "sub"):
            res = self.run_script([str(target)])
            self.assertEqual(res.returncode, 1)
            self.assertIn("separate-git-dir", res.stderr)
            self.assertNotIn(f'"{elsewhere}"', res.stderr)
            self.assertFalse((target / ".agents").exists())

        res_sep_wt = self.run_script([str(sep_wt)])
        self.assertEqual(res_sep_wt.returncode, 1)
        self.assertNotIn("re-run on the main root", res_sep_wt.stderr)
        self.assertNotIn(f'"{elsewhere}"', res_sep_wt.stderr)
        self.assertFalse((sep_wt / ".agents").exists())

    def test_ancestor_dot_git_without_git_binary_or_broken_gitfile_refused(self) -> None:
        # 1. Ancestor .git present, but PATH has no git binary
        repo = self.init_git_repo(self.root / "no_git_bin_repo")
        sub = repo / "deep" / "sub"
        sub.mkdir(parents=True)
        no_git_bin = self.make_posix_bin_without_git()

        res_no_git = self.run_script([str(sub)], env_extra={"PATH": str(no_git_bin)})
        self.assertEqual(res_no_git.returncode, 1)
        self.assertIn("refusing to fall back to non-Git mode", res_no_git.stderr)
        self.assertFalse((sub / ".agents").exists())
        self.assertFalse(self.claude_dir.exists())

        # 2. Broken .git gitfile
        broken = self.root / "broken_gitfile_repo"
        broken.mkdir()
        (broken / ".git").write_text("gitdir: /nonexistent/corrupt/path\n", encoding="utf-8")
        res_broken = self.run_script([str(broken)])
        self.assertEqual(res_broken.returncode, 1)
        self.assertIn("refusing to fall back to non-Git mode", res_broken.stderr)
        self.assertFalse((broken / ".agents").exists())

    def test_symlinked_repo_path_resolves_physical_path(self) -> None:
        real_repo = self.init_git_repo(self.root / "real_physical_repo")
        link_repo = self.root / "symlink_to_repo"
        os.symlink(real_repo, link_repo)

        gmem = self.global_project_dir(real_repo) / "memory"
        gmem.mkdir(parents=True)
        (gmem / "MEMORY.md").write_text("via symlink\n", encoding="utf-8")

        res = self.run_script([str(link_repo)])
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertTrue(gmem.is_symlink())
        self.assertEqual(os.path.realpath(str(gmem)), str(real_repo / ".agents" / "memory"))
        raw_link_project_dir = self.claude_dir / "projects" / encode_expected_ascii(str(link_repo))
        self.assertFalse(raw_link_project_dir.exists())

    # ------------------------------------------------------------------
    # Group 5: Non-Git directory & SVN paths (2 core + 1 optional real SVN)
    # ------------------------------------------------------------------

    def test_plain_non_git_directory_migration(self) -> None:
        plain = self.root / "plain_dir"
        plain.mkdir()
        gproj = self.global_project_dir(plain)
        gmem = gproj / "memory"
        gmem.mkdir(parents=True)
        (gmem / "MEMORY.md").write_text("plain dir note\n", encoding="utf-8")

        res = self.run_script([str(plain)])
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("No .git or .svn detected", res.stdout)
        self.assertTrue(gmem.is_symlink())
        self.assertFalse((gproj / "memory.bak").exists())
        self.assertEqual((plain / ".agents" / "memory" / "MEMORY.md").read_text(encoding="utf-8"), "plain dir note\n")

    def _write_fake_svn(
        self,
        bin_dir: Path,
        log_file: Path,
        mode: str,
        wc_root: Optional[Path] = None,
    ) -> None:
        svn_bin = bin_dir / "svn"
        wc_root_str = str(wc_root) if wc_root is not None else ""
        script = f"""#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$*" >> "{log_file}"
cmd="${{1:-}}"
shift || true
case "$cmd" in
  info)
    if [[ -n "{wc_root_str}" && "$*" == *"--show-item wc-root"* ]]; then
      printf '%s\\n' "{wc_root_str}"
    fi
    ;;
  status)
    mode="{mode}"
    case "$mode" in
      normal)
        if [[ "$*" == *"-v --no-ignore --depth empty"* ]]; then
          printf '                 2        2 tester %s\\n' "${{@: -1}}"
        elif [[ "$*" == *"--no-ignore --depth empty"* ]]; then
          printf ''
        elif [[ "$*" == *"-q --depth empty"* ]]; then
          printf 'A       %s\\n' "${{@: -1}}"
        elif [[ "$*" == *"-q"* ]]; then
          printf 'A       %s\\n' "${{@: -1}}"
        fi
        ;;
      preexisting_dirty)
        printf 'M       %s/MEMORY.md\\n' "${{@: -1}}"
        ;;
      dir_ignored)
        if [[ "$*" == *"--no-ignore --depth empty"* ]]; then
          printf 'I       %s\\n' "${{@: -1}}"
        fi
        ;;
      only_subdir_tracked)
        if [[ "$*" == *"-v --no-ignore --depth empty"* ]]; then
          printf 'I                                        %s\\n' "${{@: -1}}"
        elif [[ "$*" == *"--no-ignore --depth empty"* ]]; then
          printf ''
        elif [[ "$*" == *"-q --depth empty"* ]]; then
          printf ''
        elif [[ "$*" == *"-q"* ]]; then
          printf ''
        fi
        ;;
    esac
    ;;
  add|commit)
    exit 0
    ;;
  *)
    exit 0
    ;;
esac
"""
        svn_bin.write_text(script, encoding="utf-8")
        svn_bin.chmod(0o755)

    def test_svn_fake_shim(self) -> None:
        # Case 1: Normal SVN migration (new .agents committed with --depth empty before .agents/memory)
        repo1 = self.root / "svn_normal"
        (repo1 / ".svn").mkdir(parents=True)
        gproj1 = self.global_project_dir(repo1)
        gmem1 = gproj1 / "memory"
        gmem1.mkdir(parents=True)
        (gmem1 / "MEMORY.md").write_text("svn note\n", encoding="utf-8")

        shim_dir1 = self.root / "svn_shim_1"
        shim_dir1.mkdir()
        log1 = self.root / "svn1.log"
        self._write_fake_svn(shim_dir1, log1, "normal", wc_root=repo1)

        res1 = self.run_script(
            [str(repo1)],
            env_extra={"PATH": f"{shim_dir1}:{os.environ.get('PATH', '')}"},
        )
        self.assertEqual(res1.returncode, 0, res1.stderr)
        self.assertFalse((gproj1 / "memory.bak").exists())
        calls1 = log1.read_text(encoding="utf-8").splitlines()
        self.assertTrue(any("status --no-ignore --depth empty" in c for c in calls1))
        commit_calls = [c for c in calls1 if c.startswith("commit ")]
        self.assertEqual(len(commit_calls), 2, f"Expected 2 svn commits, got: {commit_calls}")
        self.assertIn("--depth empty", commit_calls[0])
        self.assertIn(".agents", commit_calls[0])
        self.assertIn(".agents/memory", commit_calls[1])

        # Case 1b: SVN rerun with pre-existing dirty memory does NOT run svn add or svn commit
        shim_dir_dirty = self.root / "svn_shim_dirty"
        shim_dir_dirty.mkdir()
        log_dirty = self.root / "svn_dirty.log"
        self._write_fake_svn(shim_dir_dirty, log_dirty, "preexisting_dirty", wc_root=repo1)
        res_dirty = self.run_script(
            [str(repo1)],
            env_extra={"PATH": f"{shim_dir_dirty}:{os.environ.get('PATH', '')}"},
        )
        self.assertEqual(res_dirty.returncode, 0, res_dirty.stderr)
        self.assertIn("WARN:", res_dirty.stderr)
        self.assertIn("Pre-existing uncommitted changes", res_dirty.stderr)
        dirty_calls = log_dirty.read_text(encoding="utf-8").splitlines()
        self.assertFalse(any(c.startswith(("add ", "commit ")) for c in dirty_calls), dirty_calls)

        # Case 1c: SVN working copy subdirectory & .svn metadata dir refused (with and without svn binary)
        sub1 = repo1 / "nested" / "sub"
        sub1.mkdir(parents=True)
        gproj_sub = self.global_project_dir(sub1)
        (gproj_sub / "memory").mkdir(parents=True)
        (gproj_sub / "memory" / "MEMORY.md").write_text("must not be deleted\n", encoding="utf-8")

        res_sub = self.run_script(
            [str(sub1)],
            env_extra={"PATH": f"{shim_dir1}:{os.environ.get('PATH', '')}"},
        )
        self.assertEqual(res_sub.returncode, 1)
        self.assertIn(str(repo1), res_sub.stderr)
        self.assertFalse((sub1 / ".agents").exists())
        self.assertTrue((gproj_sub / "memory" / "MEMORY.md").exists())

        no_vcs_bin = self.make_posix_bin_without_git()
        res_sub_nosvn = self.run_script([str(sub1)], env_extra={"PATH": str(no_vcs_bin)})
        self.assertEqual(res_sub_nosvn.returncode, 1)
        self.assertIn(str(repo1), res_sub_nosvn.stderr)
        self.assertFalse((sub1 / ".agents").exists())

        res_dotsvn = self.run_script([str(repo1 / ".svn")])
        self.assertEqual(res_dotsvn.returncode, 1)
        self.assertIn(".svn metadata", res_dotsvn.stderr)

        # Case 2: .agents/memory/ ignored at directory level (--depth empty) -> WARN + exit 0 + backup preserved
        repo2 = self.root / "svn_ignored_dir"
        (repo2 / ".svn").mkdir(parents=True)
        gproj2 = self.global_project_dir(repo2)
        gmem2 = gproj2 / "memory"
        gmem2.mkdir(parents=True)
        (gmem2 / "MEMORY.md").write_text("svn ignored\n", encoding="utf-8")

        shim_dir2 = self.root / "svn_shim_2"
        shim_dir2.mkdir()
        log2 = self.root / "svn2.log"
        self._write_fake_svn(shim_dir2, log2, "dir_ignored", wc_root=repo2)

        res2 = self.run_script(
            [str(repo2)],
            env_extra={"PATH": f"{shim_dir2}:{os.environ.get('PATH', '')}"},
        )
        self.assertEqual(res2.returncode, 0, res2.stderr)
        self.assertIn("WARN:", res2.stderr)
        self.assertTrue((gproj2 / "memory.bak").is_dir())

        # Case 3: Subdir exists, all regular files ignored -> untracked -> exit 1 + backup preserved
        repo3 = self.root / "svn_files_ignored"
        (repo3 / ".svn").mkdir(parents=True)
        gproj3 = self.global_project_dir(repo3)
        gmem3 = gproj3 / "memory"
        (gmem3 / "sub").mkdir(parents=True)
        (gmem3 / "sub" / "MEMORY.md").write_text("ignored file\n", encoding="utf-8")

        shim_dir3 = self.root / "svn_shim_3"
        shim_dir3.mkdir()
        log3 = self.root / "svn3.log"
        self._write_fake_svn(shim_dir3, log3, "only_subdir_tracked", wc_root=repo3)

        res3 = self.run_script(
            [str(repo3)],
            env_extra={"PATH": f"{shim_dir3}:{os.environ.get('PATH', '')}"},
        )
        self.assertEqual(res3.returncode, 1)
        self.assertIn("0 regular files are tracked", res3.stderr)
        self.assertTrue((gproj3 / "memory.bak").is_dir())

    @unittest.skipUnless(HAS_REAL_SVN, "svn and svnadmin are required for real SVN test")
    def test_svn_real_repository(self) -> None:
        env = self.base_env()
        svn_repo = self.root / "svn_central"
        subprocess.run(
            ["svnadmin", "create", str(svn_repo)],
            env=env,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        wc = self.root / "svn_wc"
        subprocess.run(
            ["svn", "checkout", f"file://{svn_repo}", str(wc)],
            env=env,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        # Create a versioned subdirectory and verify subdirectory invocation is refused
        ver_sub = wc / "subpkg"
        ver_sub.mkdir()
        subprocess.run(["svn", "add", str(ver_sub)], env=env, check=True, stdout=subprocess.PIPE)
        subprocess.run(
            ["svn", "commit", str(ver_sub), "-m", "add subpkg"],
            env=env,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        res_sub = self.run_script([str(ver_sub)])
        self.assertEqual(res_sub.returncode, 1)
        self.assertIn(str(wc), res_sub.stderr)
        self.assertFalse((ver_sub / ".agents").exists())

        # Unrelated local edit in WC must not be committed
        (wc / "unrelated.txt").write_text("do not commit\n", encoding="utf-8")
        subprocess.run(["svn", "add", str(wc / "unrelated.txt")], env=env, check=True, stdout=subprocess.PIPE)

        gproj = self.global_project_dir(wc)
        gmem = gproj / "memory"
        gmem.mkdir(parents=True)
        (gmem / "MEMORY.md").write_text("real svn memory\n", encoding="utf-8")

        res = self.run_script([str(wc)])
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertTrue(gmem.is_symlink())
        self.assertFalse((gproj / "memory.bak").exists())

        st_lines = subprocess.run(
            ["svn", "status", str(wc)],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=True,
        ).stdout.splitlines()
        self.assertTrue(
            any(line.startswith("A") and line.rstrip().endswith("unrelated.txt") for line in st_lines),
            f"Expected unrelated.txt to remain locally added, got: {st_lines}",
        )

        central_files = subprocess.run(
            ["svn", "list", "-R", f"file://{svn_repo}"],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=True,
        ).stdout.splitlines()
        self.assertIn(".agents/memory/MEMORY.md", central_files)
        self.assertNotIn("unrelated.txt", central_files)

        # Idempotent rerun on clean tracked files
        res2 = self.run_script([str(wc)])
        self.assertEqual(res2.returncode, 0, res2.stderr)


if __name__ == "__main__":
    unittest.main()
