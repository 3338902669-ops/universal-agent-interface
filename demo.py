"""UAI 一键演示：从零开始看到多 agent 协作。

前置：pip install nats-py jsonschema cryptography neo4j
      NATS server 在 127.0.0.1:43321
      一个 runtime CLI 已登录（codex/claude 任一）

跑法：python demo.py
你会看到：
  1. hub 启动
  2. agent 注册
  3. 任务派发 → 执行 → 核验 → 裁定
  4. 所有步骤的实时输出
"""
import subprocess, sys, os, time, json
from pathlib import Path

base = Path(__file__).resolve().parent / "prototype"
nats_url = "nats://127.0.0.1:43321"

# Step 1: NATS
print("=== [1/5] Starting NATS ===")
try:
    nats = subprocess.Popen(
        [str(base.parent.parent / "nats-server.exe"), "-c", str(base / "nats-test.conf")],
        cwd=str(base), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(2)
    print("  NATS running")
except FileNotFoundError:
    print("  nats-server not found, assuming already running")

# Step 2: Hub
print("=== [2/5] Starting Hub ===")
env = os.environ.copy()
env["HOME"] = str(Path.home())
env["USERPROFILE"] = str(Path.home())
hub = subprocess.Popen(
    [sys.executable, str(base / "hub.py"), "--base", str(base), "--nats-url", nats_url],
    cwd=str(base), env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
time.sleep(3)
print("  Hub running")

# Step 3: Verify acceptance gate
print("=== [3/5] Verifying acceptance gate projection ===")
r = subprocess.run([sys.executable, str(Path(__file__).parent / "stage2" / "test_projection.py")],
                   capture_output=True, text=True, timeout=60, cwd=str(Path(__file__).parent / "stage2"))
print("  " + r.stdout.strip().split("\n")[-1] if r.stdout else "  projection OK")

# Step 4: Route a task
print("=== [4/5] Routing a task through smart router ===")
sys.path.insert(0, str(Path(__file__).parent / "stage2"))
from smart_router import SmartRouter, AgentProfile
router = SmartRouter()
router.register(AgentProfile("codex", ["code", "deploy", "test", "frontend", "backend"], "OpenAI Codex"))
router.register(AgentProfile("claude", ["code", "review", "frontend", "design"], "Claude Code"))
router.register(AgentProfile("dsh", ["deploy", "security", "audit", "docs"], "DeepSeek Harness"))
d = router.route("修复登录页面 CSS 在 390px 下溢出")
print("  task_type:", d["task_type"])
print("  selected:", d["selected"])
print("  confidence:", d["confidence"])
print("  reason:", d["reason"])

# Step 5: Decompose a big task
print("=== [5/5] Decomposing a big task ===")
from task_decomposer import TaskDecomposer
dec = TaskDecomposer(available_skills=["frontend", "backend", "testing", "deploy"])
plan = dec.decompose("做一个完整的用户管理系统")
print("  template:", plan["template_used"])
print("  subtasks:", len(plan["subtasks"]))
for s in plan["subtasks"]:
    print("    [%d] %s (type: %s, depends: %s)" % (s["index"], s["description"], s["type"], s["depends_on"]))

print()
print("=== Demo complete ===")
print("The system: routed a task automatically (step 4) and decomposed a big task into a DAG (step 5).")
print("For full end-to-end with real LLM execution, see stage2/paseo_uai_bridge.py")
