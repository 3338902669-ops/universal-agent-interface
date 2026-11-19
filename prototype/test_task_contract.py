"""任务契约回归：把派单格式错误挡在 hub 之前，不靠 agent 临场猜测。"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from core import validate_envelope  # noqa: E402
from lab_client import build_envelope  # noqa: E402


class TaskContractTests(unittest.TestCase):
    def contract(self, root: Path, **overrides):
        value = {
            "version": 1,
            "scope": "只审指定函数，不改文件",
            "allowed_paths": [str((root / "module.py").resolve())],
            "forbidden_actions": ["不得修改文件", "不得读取技能库", "不得全仓扫描"],
            "response_format": "第一行 PASS 或 FAIL；随后最多三条风险，每条附文件:行号",
            "time_limit_s": 60,
        }
        value.update(overrides)
        return value

    def envelope(self, root: Path, **overrides):
        contract = self.contract(root, **overrides)
        return build_envelope(
            instruction="审查指定变更并给出结论",
            interface_id="codex",
            capability=None,
            timeout_s=120,
            params={"task_contract": contract},
        )

    def test_v1_envelope_without_optional_fields_is_accepted(self):
        import uuid
        from core import now_iso
        env = {
            "contract_version": "1.0",
            "envelope_id": str(uuid.uuid4()),
            "task_id": str(uuid.uuid4()),
            "idempotency_key": "v1-minimal",
            "target": {"interface_id": "codex"},
            "instruction": "单行任务",
            "timeout_s": 30,
            "cls": "readonly",
            "created_by": "contract-test",
            "created_at": now_iso(),
            "trace_id": str(uuid.uuid4()),
        }
        ok, reason, _ = validate_envelope(env)
        self.assertTrue(ok, reason)

    def test_valid_contract_is_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "module.py").write_text("x = 1", encoding="utf-8")
            ok, reason, _ = validate_envelope(self.envelope(root))
        self.assertTrue(ok, reason)

    def test_contract_rejects_relative_allowed_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "module.py").write_text("x = 1", encoding="utf-8")
            ok, reason, _ = validate_envelope(self.envelope(root, allowed_paths=["module.py"]))
        self.assertFalse(ok)
        self.assertEqual(reason, "invalid_task_contract:allowed_path_not_absolute")

    def test_contract_rejects_multiline_instruction(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env = self.envelope(root)
            env["instruction"] = "第一行\n第二行"
            ok, reason, _ = validate_envelope(env)
        self.assertFalse(ok)
        self.assertEqual(reason, "invalid_task_contract:instruction_must_be_single_line")

    def test_contract_rejects_nonfinite_time_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "module.py").write_text("x = 1", encoding="utf-8")
            ok, reason, _ = validate_envelope(self.envelope(root, time_limit_s=float("nan")))
        self.assertFalse(ok)
        self.assertEqual(reason, "invalid_task_contract:invalid_time_limit")

    def test_contract_rejects_budget_above_envelope_timeout(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "module.py").write_text("x = 1", encoding="utf-8")
            ok, reason, _ = validate_envelope(self.envelope(root, time_limit_s=121))
        self.assertFalse(ok)
        self.assertEqual(reason, "invalid_task_contract:time_limit_exceeds_envelope")

    def test_contract_prompt_is_one_line_and_contains_constraints(self):
        from dispatch_contract import build_runtime_instruction
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "module.py").write_text("x = 1", encoding="utf-8")
            instruction = build_runtime_instruction(
                self.envelope(root),
                "【UAI 协作上下文】席位 codex。\n不得复述前言。",
            )
        self.assertNotIn("\n", instruction)
        self.assertIn("绝对路径", instruction)
        self.assertIn("module.py", instruction)
        self.assertIn("不得读取技能库", instruction)
        self.assertIn("第一行 PASS 或 FAIL", instruction)


if __name__ == "__main__":
    unittest.main(verbosity=2)
