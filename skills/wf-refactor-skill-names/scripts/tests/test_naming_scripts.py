#!/usr/bin/env python3
"""Offline tests for audit_skill_names.py, rename_skill.py and check_skill_name_hook.py.

Each test runs in a throwaway HOME with its own ownership.md, so nothing on disk is read
or changed. Run:  python3 -I ~/.claude/skills/wf-refactor-skill-names/scripts/tests/test_naming_scripts.py
"""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent
TEAMMATE = {"GIT_AUTHOR_NAME": "Teammate", "GIT_AUTHOR_EMAIL": "mate@example.com",
            "GIT_COMMITTER_NAME": "Teammate", "GIT_COMMITTER_EMAIL": "mate@example.com"}


def skill(folder, name, body="Body.\n"):
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text(f"---\nname: {name}\ndescription: test\n---\n\n{body}", encoding="utf-8")


class Sandbox(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name) / "home"
        self.cfg = Path(self._tmp.name) / "cfg"
        self.home.mkdir()
        self.cfg.mkdir()
        (self.cfg / "projects.md").write_text("| `pz` | Pazzy |\n", encoding="utf-8")
        self.env = {**os.environ, "HOME": str(self.home), "SKILL_NAMES_CONFIG_DIR": str(self.cfg),
                    "GIT_CONFIG_NOSYSTEM": "1"}
        self.write_ownership([])

    def tearDown(self):
        self._tmp.cleanup()

    def write_ownership(self, mine):
        (self.cfg / "ownership.md").write_text(
            "# Skill ownership\n\n## Me\n\n- tester\n\n## Mine\n\n" + "".join(f"- {m}\n" for m in mine)
            + "\n## External\n\n", encoding="utf-8")

    def run_py(self, script, *args, stdin=None):
        return subprocess.run([sys.executable, "-I", str(SCRIPTS / script), *args], input=stdin,
                              capture_output=True, text=True, env=self.env)

    def git(self, repo, *args, env=None):
        r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                           env={**self.env, **(env or {})})
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout

    def audit_owner(self, skill_dir):
        code = ("import importlib.util,sys;"
                f"s=importlib.util.spec_from_file_location('a',{str(SCRIPTS / 'audit_skill_names.py')!r});"
                "a=importlib.util.module_from_spec(s);s.loader.exec_module(a);"
                "me,mine,ext=a.read_ownership();"
                f"from pathlib import Path;d=Path({str(skill_dir)!r});"
                "print(a.owner(d,me,mine,ext,a.lock_entries(d.parent)))")
        r = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, env=self.env)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.strip()


class OwnerAfterRename(Sandbox):
    def make_repo(self):
        repo = self.home / "proj" / ".claude"
        skill(repo / "skills" / "old-helper", "old-helper")
        subprocess.run(["git", "init", "-q", str(repo)], check=True, env=self.env)
        self.git(repo, "config", "user.name", "Tester")
        self.git(repo, "config", "user.email", "tester@example.com")
        self.git(repo, "add", ".")
        self.git(repo, "commit", "-qm", "add helper", env=TEAMMATE)
        return repo

    def test_staged_rename_keeps_original_owner(self):
        repo = self.make_repo()
        self.git(repo, "mv", "skills/old-helper", "skills/wf-helper")
        self.assertEqual(self.audit_owner(repo / "skills" / "wf-helper"), "ext:Teammate")

    def test_committed_rename_follows_history(self):
        repo = self.make_repo()
        self.git(repo, "mv", "skills/old-helper", "skills/wf-helper")
        self.git(repo, "commit", "-qm", "rename")
        self.assertEqual(self.audit_owner(repo / "skills" / "wf-helper"), "ext:Teammate")


class HookCrash(Sandbox):
    def test_crash_is_reported_not_hidden(self):
        sdir = self.home / ".claude" / "skills"
        skill(sdir / "wf-thing", "wf-thing")
        (self.cfg / "ownership.md").write_text("## Mine\n\n- [unclosed\n", encoding="utf-8")
        (self.cfg / "projects.md").unlink()
        os.chmod(sdir / "wf-thing" / "SKILL.md", 0)
        try:
            payload = json.dumps({"tool_input": {"file_path": str(sdir / "wf-thing" / "SKILL.md")},
                                  "tool_response": {"type": "update"}})
            r = self.run_py("check_skill_name_hook.py", stdin=payload)
        finally:
            os.chmod(sdir / "wf-thing" / "SKILL.md", 0o644)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Skill name check crashed", json.loads(r.stdout)["systemMessage"])

    def test_unrelated_file_is_silent(self):
        r = self.run_py("check_skill_name_hook.py", stdin=json.dumps({"tool_input": {"file_path": "/tmp/x.md"}}))
        self.assertEqual((r.returncode, r.stdout), (0, ""))


class RenameSkill(Sandbox):
    def setUp(self):
        super().setUp()
        self.sdir = self.home / ".claude" / "skills"
        skill(self.sdir / "old-tool", "old-tool", "Run /old-tool to do it.\n")
        skill(self.sdir / "wf-caller", "wf-caller", "Then call the `old-tool` skill.\n")
        skill(self.sdir / "kn-theirs", "kn-theirs", "Mentions old-tool.\n")
        (self.sdir / "kn-theirs" / "SKILL.md").write_text(
            "---\nname: kn-theirs\nauthor: Someone Else\n---\n\nMentions old-tool.\n", encoding="utf-8")
        (self.home / ".claude" / "settings.json").write_text('{"allow": ["Skill(old-tool)"]}', encoding="utf-8")
        self.write_ownership(["~/.claude/skills/old-tool", "~/.claude/skills/wf-caller"])

    def rename(self, *extra):
        return self.run_py("rename_skill.py", "--skills-dir", str(self.sdir), "--old", "old-tool",
                           "--new", "tl-tool", *extra)

    def test_dry_run_changes_nothing(self):
        r = self.rename()
        self.assertEqual(r.returncode, 0, r.stderr)
        plan = json.loads(r.stdout)[0]
        edit = {Path(e["file"]).name if "skills" not in e["file"] else Path(e["file"]).parent.name
                for e in plan["references"]["edit"]}
        self.assertEqual(edit, {"old-tool", "wf-caller", "settings.json"})
        self.assertEqual([Path(e["file"]).parent.name for e in plan["references"]["external"]], ["kn-theirs"])
        self.assertTrue((self.sdir / "old-tool").is_dir())
        self.assertFalse(plan["applied"])

    def test_apply_moves_and_rewrites(self):
        r = self.rename("--apply")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse((self.sdir / "old-tool").exists())
        md = (self.sdir / "tl-tool" / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("name: tl-tool", md)
        self.assertIn("/tl-tool", md)
        self.assertIn("`tl-tool` skill", (self.sdir / "wf-caller" / "SKILL.md").read_text(encoding="utf-8"))
        self.assertIn("Skill(tl-tool)", (self.home / ".claude" / "settings.json").read_text(encoding="utf-8"))
        self.assertIn("old-tool", (self.sdir / "kn-theirs" / "SKILL.md").read_text(encoding="utf-8"))
        own = (self.cfg / "ownership.md").read_text(encoding="utf-8")
        self.assertIn("- ~/.claude/skills/tl-tool\n", own)
        self.assertNotIn("old-tool", own)

    def test_refuses_existing_target_and_others_skills(self):
        skill(self.sdir / "tl-tool", "tl-tool")
        r = self.rename("--apply")
        self.assertEqual(r.returncode, 1)
        self.assertIn("already exists", r.stdout)
        self.assertTrue((self.sdir / "old-tool").is_dir())
        r = self.run_py("rename_skill.py", "--skills-dir", str(self.sdir), "--old", "kn-theirs",
                        "--new", "kn-mine", "--apply")
        self.assertEqual(r.returncode, 1)
        self.assertIn("not mine", r.stdout)

    def test_refuses_bad_new_name(self):
        r = self.run_py("rename_skill.py", "--skills-dir", str(self.sdir), "--old", "old-tool", "--new", "tool")
        self.assertEqual(r.returncode, 1)
        self.assertIn("fails the naming check", r.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
