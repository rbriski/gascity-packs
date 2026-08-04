from __future__ import annotations

import importlib.util
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[2]
AUDIT_PATH = REPO_ROOT / "gstack/skills/gstack-lite/scripts/audit_city.py"
VALID_PACK = """\
[pack]
name = "city"
schema = 2

[imports.gstack]
source = "gstack"
"""
VALID_CITY = """\
[agent_defaults]
append_fragments = ["gstack-lite-policy"]

[providers.sol-fast]
base = "builtin:codex"
[providers.luna-economy]
base = "builtin:codex"
[providers.claude-careful]
base = "builtin:claude"
[providers.claude-review]
base = "builtin:claude"
[providers.sol-rescue]
base = "builtin:codex"

[defaults.rig.imports.gc]
source = "gc"

[[patches.agent]]
name = "gc.implementation-reviewer"
provider = "claude-review"
max_active_sessions = 1

[[rigs]]
name = "demo"
suspended_on_start = true
max_active_sessions = 5
[rigs.imports.gc]
source = "gc"

[[rigs.patches]]
agent = "polecat"
[rigs.patches.pool]
max = 2
"""


def load_audit_module():
    spec = importlib.util.spec_from_file_location("gstack_lite_audit", AUDIT_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load {AUDIT_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_city(root: Path, city_toml: str = VALID_CITY, pack_toml: str = VALID_PACK) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "city.toml").write_text(city_toml, encoding="utf-8")
    (root / "pack.toml").write_text(pack_toml, encoding="utf-8")
    return root


class GstackLiteContractTests(unittest.TestCase):
    def test_skill_is_concise_and_owns_generic_delivery_triggers(self) -> None:
        skill = (REPO_ROOT / "gstack/skills/gstack-lite/SKILL.md").read_text(encoding="utf-8")
        self.assertIn("name: gstack-lite", skill)
        self.assertIn("Use by default for Gas City requests", skill)
        self.assertIn("Never install or launch the deprecated Complete Delivery pack", skill)
        self.assertNotIn("TODO", skill)
        self.assertLess(len(skill.splitlines()), 180)

    def test_mayor_and_prompt_fragment_default_to_gstack_lite(self) -> None:
        mayor = (REPO_ROOT / "gascity/skills/mayor/SKILL.md").read_text(encoding="utf-8")
        fragment = (
            REPO_ROOT / "gascity/template-fragments/gstack-lite-policy.template.md"
        ).read_text(encoding="utf-8")
        self.assertIn("## Default Delivery Policy", mayor)
        self.assertIn("launch the deprecated Complete Delivery pack", mayor)
        self.assertIn('{{ define "gstack-lite-policy" -}}', fragment)
        self.assertIn("one independent review", fragment)

    def test_complete_delivery_is_historical_only(self) -> None:
        skill = (
            REPO_ROOT / "complete-delivery/skills/complete-delivery/SKILL.md"
        ).read_text(encoding="utf-8")
        readme = (REPO_ROOT / "complete-delivery/README.md").read_text(encoding="utf-8")
        registry = (REPO_ROOT / "registry.toml").read_text(encoding="utf-8")
        self.assertIn("Historical guidance for the deprecated Complete Delivery pack", skill)
        self.assertIn("never use for ordinary build", skill)
        self.assertIn("ARCHIVED / DEPRECATED", readme)
        self.assertIn("DEPRECATED historical pack", registry)

    def test_audit_removes_only_exact_stale_skill_symlinks(self) -> None:
        audit_module = load_audit_module()
        with tempfile.TemporaryDirectory() as tmp:
            city = write_city(Path(tmp))
            skill_dir = city / ".gc/agents/demo/.codex/skills"
            skill_dir.mkdir(parents=True)
            stale = skill_dir / "complete-delivery.complete-delivery"
            stale.symlink_to("/cache/complete-delivery/skills/complete-delivery")
            unrelated = skill_dir / "keep-me"
            unrelated.symlink_to("/cache/gstack/skills/review")
            same_name_unrelated_target = (
                city / ".agents/skills/complete-delivery.complete-delivery"
            )
            same_name_unrelated_target.parent.mkdir(parents=True)
            same_name_unrelated_target.symlink_to("/cache/gstack/skills/review")
            regular_same_name = (
                city / ".claude/skills/complete-delivery.complete-delivery"
            )
            regular_same_name.parent.mkdir(parents=True)
            regular_same_name.write_text("preserve me", encoding="utf-8")

            errors, _ = audit_module.audit(city, fix_stale_skills=False)
            self.assertIn("1 stale Complete Delivery skill symlink(s) remain", errors)

            errors, notes = audit_module.audit(city, fix_stale_skills=True)
            self.assertEqual(errors, [])
            self.assertTrue(any("removed stale skill symlink" in note for note in notes))
            self.assertFalse(stale.exists())
            self.assertTrue(unrelated.is_symlink())
            self.assertTrue(same_name_unrelated_target.is_symlink())
            self.assertTrue(regular_same_name.is_file())

    def test_audit_reports_each_configuration_guard(self) -> None:
        audit_module = load_audit_module()
        city_without_rig_table = "rigs = [\"demo\"]\n" + VALID_CITY.split("[[rigs]]", 1)[0]
        cases = [
            (
                "missing gstack import",
                VALID_CITY,
                "[pack]\nname = \"city\"\nschema = 2\n",
                "city pack must import gstack",
            ),
            (
                "active Complete Delivery import",
                VALID_CITY,
                VALID_PACK + "\n[imports.complete-delivery]\nsource = \"legacy\"\n",
                "city pack still contains an active Complete Delivery import",
            ),
            (
                "Complete Delivery city setting",
                VALID_CITY + "\n[metadata]\npolicy = \"complete-delivery\"\n",
                VALID_PACK,
                "city.toml still contains active Complete Delivery configuration",
            ),
            (
                "missing provider",
                VALID_CITY.replace(
                    '[providers.sol-fast]\nbase = "builtin:codex"\n', ""
                ),
                VALID_PACK,
                "missing provider aliases: sol-fast",
            ),
            (
                "missing policy fragment",
                VALID_CITY.replace(
                    'append_fragments = ["gstack-lite-policy"]',
                    "append_fragments = []",
                ),
                VALID_PACK,
                "agent_defaults.append_fragments must include gstack-lite-policy",
            ),
            (
                "missing future rig role",
                VALID_CITY.replace(
                    "[defaults.rig.imports.gc]", "[defaults.rig.imports.other]"
                ),
                VALID_PACK,
                "future rigs must inherit the gc roles import",
            ),
            (
                "missing current rig role",
                VALID_CITY.replace("[rigs.imports.gc]", "[rigs.imports.other]"),
                VALID_PACK,
                "current rigs missing explicit gc roles import: demo",
            ),
            (
                "missing reviewer patch",
                VALID_CITY.replace(
                    'name = "gc.implementation-reviewer"', 'name = "other"'
                ),
                VALID_PACK,
                "one global Claude gc.implementation-reviewer patch is required",
            ),
            (
                "invalid rigs shape",
                city_without_rig_table,
                VALID_PACK,
                "city.toml rigs must be an array of tables",
            ),
            (
                "unsafe startup",
                VALID_CITY.replace("suspended_on_start = true", "suspended_on_start = false"),
                VALID_PACK,
                "current rigs must set suspended_on_start=true: demo",
            ),
            (
                "unsafe total session cap",
                VALID_CITY.replace("max_active_sessions = 5", "max_active_sessions = 6"),
                VALID_PACK,
                "current rigs must cap max_active_sessions between 1 and 5: demo",
            ),
            (
                "unsafe writer cap",
                VALID_CITY.replace("max = 2", "max = 3"),
                VALID_PACK,
                "current rigs must set one polecat pool cap between 1 and 2: demo",
            ),
        ]

        for label, city_toml, pack_toml, expected in cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as tmp:
                city = write_city(Path(tmp), city_toml, pack_toml)
                errors, _ = audit_module.audit(city, fix_stale_skills=False)
                self.assertIn(expected, errors)

    def test_audit_handles_missing_and_malformed_city_files(self) -> None:
        audit_module = load_audit_module()
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "missing"
            errors, _ = audit_module.audit(missing, fix_stale_skills=False)
            self.assertIn("is not a Gas City root", errors[0])

            malformed = write_city(Path(tmp) / "malformed", city_toml="[")
            errors, _ = audit_module.audit(malformed, fix_stale_skills=False)
            self.assertIn("cannot read", errors[0])
            self.assertIn("city.toml", errors[0])

    def test_cleanup_reports_unlink_failure_and_preserves_link(self) -> None:
        audit_module = load_audit_module()
        with tempfile.TemporaryDirectory() as tmp:
            city = write_city(Path(tmp))
            stale = city / ".codex/skills/complete-delivery.complete-delivery"
            stale.parent.mkdir(parents=True)
            stale.symlink_to("/cache/complete-delivery/skills/complete-delivery")
            original_unlink = Path.unlink

            def fail_stale_unlink(path: Path, *args, **kwargs):
                if path == stale:
                    raise PermissionError("test denial")
                return original_unlink(path, *args, **kwargs)

            with mock.patch.object(audit_module.Path, "unlink", fail_stale_unlink):
                errors, _ = audit_module.audit(city, fix_stale_skills=True)

            self.assertTrue(stale.is_symlink())
            self.assertTrue(
                any("could not remove stale skill symlink" in error for error in errors)
            )

    def test_cli_exit_codes_and_fail_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            city = write_city(Path(tmp))
            passed = subprocess.run(
                [sys.executable, str(AUDIT_PATH), "--city", str(city)],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(passed.returncode, 0, passed.stderr)
            self.assertIn("PASS Gstack Lite city contract", passed.stdout)

            (city / "city.toml").write_text("[", encoding="utf-8")
            failed = subprocess.run(
                [sys.executable, str(AUDIT_PATH), "--city", str(city)],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(failed.returncode, 1)
            self.assertIn("FAIL cannot read", failed.stderr)
            self.assertNotIn("Traceback", failed.stderr)


if __name__ == "__main__":
    unittest.main()
