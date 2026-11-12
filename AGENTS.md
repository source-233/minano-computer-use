# minano-computer-use

独立的 Windows / Python MCP 项目，工作目录是 `D:/project/minano-computer-use`。
本会话从 ReShade 项目迁来；ReShade、游戏 hook、采集插件不是本项目范围。

## 产品约束

- 保留 sshh12 的顺手理念：按名称定位、批量动作、文字与截图分离、动作录制。
- 重新实现，不直接修改 uv 全局安装的源码或复制旧项目。全局 minano 由已构建 wheel 安装，更新须先验证再安装；sshh12 已按用户要求移除。
- 默认严格 UIA：不主动激活窗口、不移动真实指针、不降级物理输入。
- UIA provider 仍可能自己激活窗口；观测并报告，不承诺绝无焦点副作用。
- 原生输入仅按每次调用的 `native` 参数选择（默认 false）；不再要求启动参数或重连。必须检查目标前台，force_focus 仍需显式动作。
- 模糊匹配、多目标、超时、SendInput 失败不能伪装成功。
- 有副作用的操作超时一律可能已生效，不自动重放；失效 UIA 进程先终止并回收。
- 不在 MCP stdout 打印日志。COM 对象只能在专属工作进程的固定线程创建和使用。
- 不默认自动启动、联网遥测、安装驱动或执行 shell；首次交付不集成任意 shell 工具。
- 0.3 全能力覆盖已获用户授权；shell/强制进程控制必须分别满足启动开关和单次调用开关，默认关闭。接口契约见 `docs/parity.md`，不以兼容为由复制旧版隐式输入回退或 eval。

## 工作方式

- `docs/architecture.md` 是模块契约；并行修改不得越过约定文件范围。
- 0.2 的扩展接口以 `docs/expansion.md` 为准；定向查询、wait_for、token、取消、预算、trace 和截图模式不得绕过既有严格策略。
- `system.cancel` 必须不经过长调用锁/默认线程池；取消请求已接收不等于松键完成。
- 不允许输出截断改变 selector 唯一性；不允许 token 失效后自动按名称重搜并执行写动作。
- WGC 为可选 extra；本机 CreateForWindow 转换失败，不得用 fallback 或 mock 宣称已验证 WGC。
- MP4 后端使用 PyAV，编码必须在可终止并回收的独立 Python 工作进程执行，不依赖外部 ffmpeg.exe；合成验证与实际应用录像是不同证据。
- 0.4 后台消息通道独立于 native：`input_mode=message` 单次明确选择，不允许物理输入/聚焦回退；组合应用状态不是原子截图，遵守 `docs/v0.4.md` 的身份、部分失败和敏感值边界。
- 不自动提交：上次第一版提交授权不等于后续每次修改可直接提交。
- 使用 `uv sync --python 3.13`；验证 `uv run ruff check .`、`uv run pytest -q`。
- 真实桌面输入不在默认测试中运行；Windows UIA 集成测试只操作自有 fixture。
- 构建/单测/真实 UIA/用户环境键鼠是不同证据，不能相互代替。
- 不提交、不暂存，除非用户明确要求。保留有意义的中文注释。
