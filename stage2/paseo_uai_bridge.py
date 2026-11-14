"""Paseo-UAI 对接器（2026-11-15，端到端已验证）。

分工：Paseo daemon 当执行编排层（harness 会话/工作区管理），UAI 当验收/账本层。
链路：UAI 信封(target.capability=paseo) -> paseo agent(真 codex runtime) ->
     waitForFinish -> reply -> 双端凭据绑定返回。

用法（需 paseo daemon 6767 + UAI hub 43321，DEEPSEEK_API_KEY 注入 daemon）：
    python paseo_uai_bridge.py "指令"
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

UAI_STAGE2 = Path(__file__).resolve().parent
PASEO_NODE = "D:/龙虾/node.exe"
PASEO_RUNNER = UAI_STAGE2 / "paseo_runner.mjs"
DEFAULT_WORKDIR = "C:/Users/33389/workspace/scratch/paseo-bridge-test"


def paseo_execute(prompt: str, workdir: str = DEFAULT_WORKDIR,
                  provider: str = "codex/default", timeout_s: int = 240) -> dict:
    """paseo 侧执行：创建 agent -> 等完成 -> 取 assistant 回复。"""
    cfg = json.dumps({"provider": provider, "cwd": workdir,
                      "prompt": prompt, "timeout_s": timeout_s})
    out = subprocess.run(
        [PASEO_NODE, str(PASEO_RUNNER), cfg],
        capture_output=True, text=True, timeout=timeout_s + 60, encoding="utf-8",
    )
    if out.returncode != 0:
        raise RuntimeError("paseo_runner failed: %s" % (out.stderr or out.stdout)[-300:])
    result = json.loads(out.stdout.strip().split("\n")[-1])
    if result.get("status") != "idle":
        raise RuntimeError("paseo agent not idle: %s" % result.get("status"))
    return result


def uai_submit(instruction: str, capability: str = "paseo",
               timeout_s: int = 300) -> dict:
    """UAI 侧受理：A2A 请求 -> 契约 v1 信封 -> hub。"""
    sys.path.insert(0, str(UAI_STAGE2))
    import inbound_bridge
    request = {"message": {"messageId": "paseo-" + uuid.uuid4().hex[:8],
                            "parts": [{"text": instruction}]}}
    return asyncio.run(inbound_bridge.submit(
        nats_url=os.environ.get("NATS_URL", "nats://127.0.0.1:43321"),
        request=request, created_by="paseo-bridge",
        target_capability=capability,
        timeout_s=timeout_s, cls="readonly", contract_version="1.0",
    ))


def bridge_once(instruction: str) -> dict:
    """完整桥接一次：UAI 受信 -> paseo 执行 -> 双端凭据绑定返回。

    验收语义：UAI 受信记录 + paseo 执行凭据（agent id + reply）绑定。要进 UAI
    账本的完整裁定链，把 bridge_once 的输出作为 VERDICT 的 basis。
    """
    uai = uai_submit(instruction)
    if not uai.get("accepted"):
        raise RuntimeError("uai_rejected: %s" % uai.get("hub_response", {}).get("reason"))
    pas = paseo_execute(instruction)
    # 订阅轮换留痕（2026-11-15）：限流检测 -> 切换决策 -> UAI 账本留痕
    sub_result = {}
    try:
        from subscription_rotator import SubscriptionRotator
        rot = SubscriptionRotator(
            UAI_STAGE2 / "data" / "subscriptions.json",
            Path(os.environ.get("UAI_ROTATION_LEDGER",
                                str(UAI_STAGE2 / "data" / "rotation-ledger.jsonl"))),
        )
        sub_result = rot.on_agent_result(pas["id"], pas.get("reply") or "",
                                         task_id=uai["task_id"])
    except Exception as e:
        sub_result = {"rotation_error": str(e)[:120]}  # 留痕层故障不阻塞主链路
    return {
        "uai_task_id": uai["task_id"],
        "uai_envelope_sha256": uai["hub_response"].get("envelope_sha256"),
        "paseo_agent_id": pas["id"],
        "reply": pas["reply"],
        "subscription": sub_result,
    }


if __name__ == "__main__":
    instruction = sys.argv[1] if len(sys.argv) > 1 else \
        "只读探测:请只回复 BRIDGE_OK 四个字符,不要执行任何命令或修改任何文件。"
    print(json.dumps(bridge_once(instruction), ensure_ascii=False, indent=1))
