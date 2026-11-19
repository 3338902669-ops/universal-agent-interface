"""UAI 协作框架核心（跨平台）。

设计边界：本包不 import 任何宿主应用、不调用 PowerShell/wscript。
平台差异只出现在 oslayer 的三个适配点：锁、进程、自启。
"""

__version__ = "0.1.0"
