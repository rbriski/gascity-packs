from __future__ import annotations

import pathlib
import tomllib
import unittest


PACK_ROOT = pathlib.Path(__file__).resolve().parents[1]
FORMULA_PATH = PACK_ROOT / "formulas" / "complete-delivery-pr-gate.formula.toml"
HANDOFF_PATH = "<artifact_root>/delivery/external-review-handoff.json"


class PrGateContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        with FORMULA_PATH.open("rb") as formula_file:
            cls.formula = tomllib.load(formula_file)
        cls.templates = {template["id"]: template for template in cls.formula["template"]}

    def test_formula_routes_every_lane_to_a_declared_agent(self) -> None:
        declared_targets = {
            f"complete-delivery.{manifest.parent.name}"
            for manifest in (PACK_ROOT / "agents").glob("*/agent.toml")
        }
        run_targets = {
            template["metadata"]["gc.run_target"]
            for template in self.formula["template"]
        }
        loop = self.templates["{target}.external-review-loop"]
        run_targets.update(
            child["metadata"]["gc.run_target"] for child in loop["children"]
        )

        self.assertEqual(
            run_targets,
            {
                "complete-delivery.external-review-resolver",
                "complete-delivery.report-editor",
            },
        )
        self.assertTrue(run_targets <= declared_targets)

    def test_formula_routes_terminal_report_update_to_report_editor(self) -> None:
        terminal = self.templates["{target}"]

        self.assertEqual(
            terminal["metadata"]["gc.run_target"],
            "complete-delivery.report-editor",
        )

    def test_formula_preserves_the_bounded_resolve_test_publish_handoff(self) -> None:
        loop = self.templates["{target}.external-review-loop"]
        self.assertEqual(loop["check"]["max_attempts"], 12)
        self.assertEqual(
            loop["check"]["check"]["path"],
            ".gc/scripts/checks/delivery-pr-approved.sh",
        )

        children = {child["id"]: child for child in loop["children"]}
        self.assertEqual(
            children["{target}.resolve-findings"]["needs"],
            ["{target}.inspect-current-head"],
        )
        self.assertEqual(
            children["{target}.rerun-local-gates"]["needs"],
            ["{target}.resolve-findings"],
        )
        self.assertEqual(
            children["{target}.publish-fixes"]["needs"],
            ["{target}.rerun-local-gates"],
        )
        for lane in (
            "{target}.resolve-findings",
            "{target}.rerun-local-gates",
            "{target}.publish-fixes",
        ):
            self.assertEqual(
                children[lane]["metadata"]["gc.continuation_group"],
                "complete-delivery-pr-fixes",
            )

        workflows = PACK_ROOT / "assets" / "workflows" / "complete-delivery-pr-gate"
        for filename in (
            "{target}.resolve-findings.md",
            "{target}.rerun-local-gates.md",
            "{target}.publish-fixes.md",
            "{target}.md",
        ):
            self.assertIn(HANDOFF_PATH, (workflows / filename).read_text(encoding="utf-8"))

    def test_resolver_prompt_keeps_publication_and_terminal_gate_in_their_lanes(self) -> None:
        prompt = (PACK_ROOT / "agents" / "external-review-resolver" / "prompt.template.md").read_text(
            encoding="utf-8"
        )

        self.assertIn("Never push\n  or resolve a thread in this lane", prompt)
        self.assertIn("resolve valid mapped threads when `published_head == tested_commit`", prompt)
        self.assertIn("Only the Formula v2 `external-review-loop` terminal check", prompt)
        self.assertNotIn("After fixes are pushed and applicable review threads are resolved", prompt)

    def test_nonterminal_lanes_only_run_local_gates_and_require_exact_published_head(self) -> None:
        workflows = PACK_ROOT / "assets" / "workflows" / "complete-delivery-pr-gate"
        rerun_local_gates = (workflows / "{target}.rerun-local-gates.md").read_text(
            encoding="utf-8"
        )
        publish_fixes = (workflows / "{target}.publish-fixes.md").read_text(
            encoding="utf-8"
        )
        prompt = (PACK_ROOT / "agents" / "external-review-resolver" / "prompt.template.md").read_text(
            encoding="utf-8"
        )

        for content in (prompt, rerun_local_gates):
            self.assertIn("complete nonterminal local-gate set", content)
            self.assertIn("delivery_gate.py", content)
            self.assertIn("delivery-pr-approved.sh", content)
            self.assertIn("approval gate before", content)
            self.assertIn("publication", content)

        self.assertIn("`published_head` is exactly equal to the\nartifact's `tested_commit`", publish_fixes)
        self.assertIn("Commit\ncontainment alone is not sufficient", publish_fixes)
        self.assertIn("next Formula iteration can inspect and retest that exact\n  refreshed head", prompt)


if __name__ == "__main__":
    unittest.main()
