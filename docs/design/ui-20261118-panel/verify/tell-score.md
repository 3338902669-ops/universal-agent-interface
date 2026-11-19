# Tell Score 记录（UI 20261118 面板）

检测器：`%LOCALAPPDATA%\Programs\Python\Python314\Scripts\ai-design-tells.exe`（本机实测版，133 tell-weight，≤20 放行）。

| 被测文件 | 分数 | 等级 | fired | 说明 |
|---|---|---|---|---|
| `preview.html`（外链 CSS） | **13.5/100** | B | 18 | 检测器不读外链样式表，把「已有 hover/focus/hairline」判成缺失（F2/E3）——**不是页面的真实状态** |
| `verify/preview-inlined.html`（CSS 内联，等价于线上注入） | **3.8/100** | **A** | 5 | 只剩 H2 |

## 剩余那条 tell 的判断

`[H2] Powered-by Theatre · names the model (+5)` —— 触发词是 `claude`。

这条在**营销页**上确实该扣：把「AI 驱动」当卖点。但本页是**能力矩阵**，它列的就是本机装了什么 agent
（dsh / codex / claude / openclaw / rivet），`claude` 在这里是**数据**，不是标签修辞。
按流程附录 D 的通用判据过四条：

1. 是内容本身要的吗 → 是（用户要看的正是「哪些 agent 就绪」）
2. 去掉会损失信息吗 → 会（少一个就绪项）
3. 是顺手默认还是想过的选择 → 想过的选择（内容来源是 `uai detect` 的真实输出）
4. 代价可控吗 → 可控（无性能/授权问题）

结论：**保留**，并在交付里写明理由，而不是为了分数删数据。

复现：

```powershell
$exe = "$env:LOCALAPPDATA\Programs\Python\Python314\Scripts\ai-design-tells.exe"
cd docs\design\ui-20261118-panel
& $exe verify\preview-inlined.html --verbose   # 期望 3.8 / A
```
