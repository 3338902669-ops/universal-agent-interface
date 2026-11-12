# a2a-official-spec（外部规范副本，只读）

`a2a.proto` 是上游 A2A 官方协议定义的**未修改副本**，用于在测试里做独立来源比对
（见 `stage2/test_projection.py` 判据 9：本契约的 `STATE_TO_A2A` 像集必须全部落在官方
`TaskState` 枚举内）。

| 项 | 值 |
|---|---|
| 上游仓 | `https://github.com/a2aproject/A2A` |
| 路径 | `specification/a2a.proto` |
| 获取方式 | `curl -L https://api.github.com/repos/a2aproject/A2A/contents/specification/a2a.proto`（base64 解码） |
| SHA-256 | `945DF6E34001B2BFD0FD62D9484B63094DFAD9D78705E41E2873441C419AE2D1` |
| 字节数 | 35942 |
| 许可 | Apache-2.0（沿用上游） |

## 为什么要入库

在此之前测试从「仓库根目录的上一级」找这份文件，于是**本机能过、CI 一直红**：
干净 runner 上不存在这个兄弟目录。把副本放进仓库后，CI 与本地用同一份来源；
`UAI_A2A_PROTO` 环境变量仍可覆盖，用于比对其它版本。

## 更新方式

替换文件后必须同步更新本文件的 SHA-256 与字节数，并重跑
`python stage2/test_projection.py`（判据 9 会读这份文件做独立比对）。
