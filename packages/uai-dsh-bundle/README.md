# @uai/dsh-bundle

DSH 里的 UAI 协作面板：左侧栏一个「UAI 协作」页，显示中枢/席位状态、一键检测、一键刷新。
后端是 UAI 核心（uai_core）；本包只做**桥接与渲染**，不复制任何协议逻辑。

## 结构

| 文件 | 作用 |
|---|---|
| lib/index.js | 宿主侧插件：在 127.0.0.1:47311 开只读 HTTP 桥，把 uai status/detect/seats 转成 JSON（含 CORS） |
| lib/client.js | 客户端插件：注册 sidebar.panellist 入口 + main 面板内容（window.__ModuleLoader__ 格式，React 由宿主提供） |
| test/host-selftest.mjs | 不装进 DSH 也能验：桥启动、CORS、status JSON、404 |
| test/tool-selftest.mjs | 验 uai_status 工具：注册、参数表、输出 schema、execute 摘要与字段 |

宿主还注册了一个工具 **uai_status**（wire 名 `uai_status`）：模型可以直接问常驻面状态，
客户端为它注册 `tool.call.toolview` 卡片 —— 这样一次调用在思考链里是一张卡（中枢/席位/监督 + 状态色），
而不是一行 JSON。

端点：/uai/ping、/uai/status、/uai/detect、/uai/seats。

## 依赖

- UAI 核心已安装：pip install -e packages/uai-core（或任意 uai 在 PATH 上）
- 插件通过环境变量 UAI_CMD 找核心，默认 uai；建议显式设成 "<python> -m uai_core"
- UAI_BASE 指向数据面（团队 base 或 ~/.uai/base）
- 桥端口可用 UAI_BRIDGE_PORT 覆盖（默认 47311）

## 安装（改 profile，需要重启 DSH 才生效）

~~~powershell
# 1) 装进当前 profile（link 方式：改代码后只需重启，不用重装）
cd $env:USERPROFILE\.dsh\profiles\desktop
pnpm add "link:<repo>\packages\uai-dsh-bundle"

# 2) 让 profile 加载这个 bundle：在 package.json 的 dsh.profile.bundles 里追加 @uai/dsh-bundle
#    然后确认 cordis.patch.yml 里对应行没有 disabled

# 3) 指定核心命令与数据面（可选但推荐）
setx UAI_CMD "C:\path\to\python.exe -m uai_core"
setx UAI_BASE "C:\Users\33389\.shared\comms-v2\cutover-base-20261109"

# 4) 重启 DSH（桌面版退出再开）
~~~

重启后左侧栏出现「UAI 协作」。右上角两个按钮：**检测并搭建**（调 uai detect）、**刷新**（重读 uai status）。

## 自检（不需要重启）

~~~powershell
cd packages\uai-dsh-bundle
$env:UAI_CMD="<python> -m uai_core"; $env:UAI_BASE="<数据面>"
node test/host-selftest.mjs
# 期望 RESULT: 5/5 passed，且 /uai/status ok=true
node test/tool-selftest.mjs
# 期望 RESULT: 10/10 passed，摘要形如「中枢 运行中 · 席位 4/4 · 监督 1」
~~~

## 卸载

~~~powershell
cd $env:USERPROFILE\.dsh\profiles\desktop
pnpm remove @uai/dsh-bundle
# 并从 package.json 的 dsh.profile.bundles 里删掉 @uai/dsh-bundle
~~~

## 已知边界（如实）

1. **面板还是只读视图**：能看、能触发检测，还不能从这里派单（派单走工具/MCP，P3 再接）；
2. **卡片只对 uai_status 生效**：其余 UAI 动作还没有卡片（等派单工具做出来再逐个加）；
3. **安装需要重启 DSH**：插件安装没有 HMR（restart-required），所以没有直接装进正在使用的 profile；
4. **桥没有鉴权**：只监听 127.0.0.1，同机进程可读；不暴露任何写操作（没有起停接口）。
