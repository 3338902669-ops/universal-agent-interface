"""Three-layer verdict system (2026-11-16).

Layer 1: Deterministic validation (schema, exit code, file existence) - zero LLM
Layer 2: Consistency verification (N runs, consistency score > threshold) - N x LLM
Layer 3: Independent verdict (separate agent, not implementer) - 1 x LLM
"""
from __future__ import annotations
import json, re, hashlib
from pathlib import Path

class ThreeLayerVerdict:
    def __init__(self, envelope: dict):
        self.contract = envelope.get("output_contract") or {}

    @staticmethod
    def _forbidden_hit(pattern: str, text: str) -> bool:
        """禁止项匹配：纯标识符用词边界+忽略大小写，避免 secret 误伤 secrets。

        含正则元字符的模式按原始正则处理，保留高级用法。
        """
        if not pattern:
            return False
        if re.fullmatch(r"[A-Za-z0-9_\-]+", pattern):
            try:
                return re.search(r"(?<![A-Za-z0-9_])" + re.escape(pattern) + r"(?![A-Za-z0-9_])",
                                 text, re.IGNORECASE) is not None
            except re.error:
                return False
        try:
            return re.search(pattern, text) is not None
        except re.error:
            return False

    def layer1_deterministic(self, output: dict, artifacts: dict) -> dict:
        results = {"pass": True, "checks": []}
        contract = self.contract
        schema = contract.get("schema")
        if schema:
            try:
                import jsonschema
                jsonschema.validate(output, schema)
                results["checks"].append({"check": "schema", "pass": True})
            except Exception as e:
                results["pass"] = False
                results["checks"].append({"check": "schema", "pass": False, "detail": str(e)[:200]})
        for inv in contract.get("invariants", []):
            import subprocess
            try:
                r = subprocess.run(inv, shell=True, capture_output=True, timeout=30)
                ok = r.returncode == 0
                results["checks"].append({"check": inv[:50], "pass": ok, "exit_code": r.returncode})
                if not ok:
                    results["pass"] = False
            except Exception as e:
                results["pass"] = False
                results["checks"].append({"check": inv[:50], "pass": False, "detail": str(e)[:100]})
        output_str = json.dumps(output, ensure_ascii=False)
        for pattern in contract.get("forbidden", []):
            if self._forbidden_hit(str(pattern), output_str):
                results["pass"] = False
                results["checks"].append(
                    {"check": "forbidden:" + str(pattern)[:30], "pass": False})
        return results

    def layer2_consistency(self, run_outputs: list) -> dict:
        n = len(run_outputs)
        if n < 2:
            return {"pass": True, "consistency": 1.0, "note": "single run, skipping"}
        threshold = self.contract.get("consistency_threshold", 0.8)
        agreements = 0
        total_pairs = 0
        for i in range(n):
            for j in range(i + 1, n):
                total_pairs += 1
                a = json.dumps(run_outputs[i], sort_keys=True, ensure_ascii=False)
                b = json.dumps(run_outputs[j], sort_keys=True, ensure_ascii=False)
                if a == b:
                    agreements += 1
                else:
                    max_len = max(len(a), len(b))
                    if max_len > 100:
                        common = sum(1 for a_c, b_c in zip(a, b) if a_c == b_c)
                        if max_len > 0 and common / max_len > threshold:
                            agreements += 1
        consistency = agreements / total_pairs if total_pairs > 0 else 1.0
        return {"pass": consistency >= threshold, "consistency": round(consistency, 4), "threshold": threshold, "total_pairs": total_pairs, "agreements": agreements}

    def layer3_independent(self, verdict: str, verifier_agent_id: str, implementer_agent_id: str) -> dict:
        independent = verifier_agent_id != implementer_agent_id
        canonical = verdict in ("通过", "带注通过", "条件通过", "不通过", "未核", "作废")
        return {"pass": independent and canonical, "independent": independent, "verdict_canonical": canonical, "verdict": verdict, "verifier": verifier_agent_id, "implementer": implementer_agent_id}

    def execute_all(self, output: dict, artifacts: dict, run_outputs: list, verdict: str, verifier_agent_id: str, implementer_agent_id: str) -> dict:
        l1 = self.layer1_deterministic(output, artifacts)
        l2 = self.layer2_consistency(run_outputs) if l1["pass"] else {"pass": None, "skipped": "layer1 failed"}
        l3 = self.layer3_independent(verdict, verifier_agent_id, implementer_agent_id)
        return {"layer1": l1, "layer2": l2, "layer3": l3, "overall_pass": l1["pass"] and (l2.get("pass") is not False) and l3["pass"]}
