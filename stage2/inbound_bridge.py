"""A2A 入站桥（A-02）：外部 A2A 请求 → 契约 v1 信封 → 中枢 `hub.submit`。

之前的状态：`inbound.py` 只把请求转成信封就停了，没有投递中枢——所以只能"展示"，不能"协作"。
本模块补上最后一段：通过 NATS request 把信封交给中枢，拿回 `task_id` 与受理结果。

不侵入 hub：中枢本来就订阅 `hub.submit` 并用 `msg.respond` 回包，这里只是当一个合法客户端。

用法（库）：
    import asyncio, inbound_bridge
    asyncio.run(inbound_bridge.submit(...))

用法（CLI）：
    python inbound_bridge.py --nats-url nats://127.0.0.1:43321 --text "跑一次只读检查"
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

import inbound

SUBMIT_SUBJECT = "hub.submit"
STATE_SUBJECT = "hub.state"


class InboundBridgeError(RuntimeError):
    pass


async def _connect(url: str):
    try:
        import nats
    except ImportError:  # pragma: no cover
        raise InboundBridgeError("nats-py 未安装：pip install nats-py")
    return await nats.connect(url, connect_timeout=5, max_reconnect_attempts=2)


async def submit(
    *,
    nats_url: str,
    request: dict,
    created_by: str,
    target_interface: str | None = None,
    target_capability: str | None = None,
    timeout_s: int = 120,
    cls: str = "readonly",
    approval: dict | None = None,
    contract_version: str = "1.0",
    connect_timeout_s: float = 10.0,
) -> dict:
    """投递一个 A2A 请求，返回 {envelope, hub_response, task_id, accepted}。"""
    envelope = inbound.envelope_from_a2a(
        request,
        created_by=created_by,
        target_interface=target_interface,
        target_capability=target_capability,
        timeout_s=timeout_s,
        cls=cls,
        approval=approval,
        contract_version=contract_version,
    )

    nc = await _connect(nats_url)
    try:
        payload = json.dumps(envelope, ensure_ascii=False).encode("utf-8")
        msg = await nc.request(SUBMIT_SUBJECT, payload, timeout=connect_timeout_s)
        hub_response = json.loads(msg.data.decode("utf-8"))
    finally:
        await nc.close()

    accepted = bool(hub_response.get("ok", False))
    task_id = hub_response.get("task_id") or envelope["task_id"]
    if not accepted:
        raise InboundBridgeError("hub_rejected: %s" % hub_response.get("reason"))
    return {
        "envelope": envelope,
        "hub_response": hub_response,
        "task_id": task_id,
        "accepted": accepted,
    }


async def query_state(*, nats_url: str, task_id: str, timeout_s: float = 5.0) -> dict:
    nc = await _connect(nats_url)
    try:
        msg = await nc.request(STATE_SUBJECT, json.dumps({"task_id": task_id}).encode(),
                               timeout=timeout_s)
        return json.loads(msg.data.decode("utf-8"))
    finally:
        await nc.close()


def _cli() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--nats-url", default=os.environ.get("NATS_URL", "nats://127.0.0.1:43321"))
    ap.add_argument("--text", required=True)
    ap.add_argument("--message-id", default="msg-00000001")
    ap.add_argument("--target", default="dsh")
    ap.add_argument("--created-by", default="a2a-client")
    ap.add_argument("--cls", default="readonly")
    ap.add_argument("--timeout-s", type=int, default=120)
    ap.add_argument("--contract-version", default="1.0")
    args = ap.parse_args()

    request = {"message": {"messageId": args.message_id, "parts": [{"text": args.text}]}}
    out = asyncio.run(submit(
        nats_url=args.nats_url, request=request, created_by=args.created_by,
        target_interface=args.target, timeout_s=args.timeout_s, cls=args.cls,
        contract_version=args.contract_version,
    ))
    print(json.dumps({k: v for k, v in out.items() if k != "envelope"}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
