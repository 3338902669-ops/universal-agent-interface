# uai-core

UAI 协作框架的跨平台内核：单例锁、进程控制、自启注册、能力检测、监督循环。

```bash
pip install -e packages/uai-core
uai doctor            # 体检：锁/端口/自启/数据面
uai detect --json     # 本机 AI agent 能力矩阵（12 个探针）
uai seats --from-detect
uai status            # 中枢/席位锁 + 进程计数（JSON）
uai serve             # 常驻监督：按 seats.yml 拉起并守护 nats/hub/席位
uai install-service   # 注册登录自启（Windows 启动文件夹 / systemd / launchd）
```

## 二级看护（guard）：监督者自己死了怎么办

`uai serve` 只负责「子进程挂了拉起来」。它自己被杀时没人管 —— 实测 DSH 桌面版重启会
把整棵 wscript → pythonw 进程树带走，常驻面全灭。所以再上层放一个最小看护：

```powershell
uai guard                  # 监督锁没人持有 → 拉起 uai serve；有人持有 → noop
uai install-guard          # 注册计划任务「UAI Guard」，默认每 2 分钟跑一次 guard
uai uninstall-guard        # 移除
```

- 判定依据是**监督锁的持有者是否存活**（不是进程名匹配，避免宿主命令行自匹配）
- `guard` 用 `pythonw` 无窗口启动，日志写 `$UAI_HOME/logs/guard.log`
- 拉起后等 `guard.verify_wait_s`（默认 6s）复查锁，确认真的活了才返回 `guard_started`
- Linux/macOS 尚未实现（返回 `unsupported_platform`），等 systemd timer / launchd 版本

实测（E1）：杀掉 supervisor（锁变 stale）→ `uai guard` → 新 supervisor 53876 →
12 秒后 resident_health 14 项全绿、4 席全部注册。
