from __future__ import annotations

import importlib.util
from pathlib import Path
import tempfile
import unittest


REPO_ROOT = Path(__file__).resolve().parents[2]
AUDIT_PATH = REPO_ROOT / "gstack/skills/gstack-lite/scripts/audit_city.py"


def load_audit_module():
    spec = importlib.util.spec_from_file_location("gstack_lite_audit", AUDIT_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load {AUDIT_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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
            city = Path(tmp)
            (city / "pack.toml").write_text(
                """[pack]\nname = \"city\"\nschema = 2\n[imports.gstack]\nsource = \"gstack\"\n""",
                encoding="utf-8",
            )
            (city / "city.toml").write_text(
                """
[agent_defaults]
append_fragments = ["gstack-lite-policy"]

[providers.sol-fast]
base = "builtin:codex"
[providers.luna-economy]
base = "builtin:codex"
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
[rigs.imports.gc]
source = "gc"
""",
                encoding="utf-8",
            )
            skill_dir = city / ".gc/agents/demo/.codex/skills"
            skill_dir.mkdir(parents=True)
            stale = skill_dir / "complete-delivery.complete-delivery"
            stale.symlink_to("/cache/complete-delivery/skills/complete-delivery")
            unrelated = skill_dir / "keep-me"
            unrelated.symlink_to("/cache/gstack/skills/review")

            errors, _ = audit_module.audit(city, fix_stale_skills=False)
            self.assertIn("1 stale Complete Delivery skill symlink(s) remain", errors)

            errors, notes = audit_module.audit(city, fix_stale_skills=True)
            self.assertEqual(errors, [])
            self.assertTrue(any("removed stale skill symlink" in note for note in notes))
            self.assertFalse(stale.exists())
            self.assertTrue(unrelated.is_symlink())


if __name__ == "__main__":
    unittest.main()
