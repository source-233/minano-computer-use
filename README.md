# minano-computer-use

用于 Windows 桌面自动化的 MCP 服务，提供 **UI Automation 控件操作**和**显式原生键鼠输入**两条通道，以及截图、录制、动作脚本、OCR 和进程管理。

默认直接操作控件，不移动真实鼠标；需要接管键鼠时，在调用中传入 `native=true`。动作提交、状态验证、失败和不确定结果分别报告，不把系统接受输入等同于任务成功。

## 环境要求

- Windows，CPython **3.13**。
- [uv](https://docs.astral.sh/uv/)：安装和运行 Python 工具。
- 支持本地 stdio MCP 的客户端。
- MP4 编码与视频验证使用随依赖安装的 **PyAV**，无需另装 `ffmpeg` 或 `ffprobe` 可执行文件。

当前限制为 CPython 3.13，因为 Windows 工作进程隔离使用了该版本的启动协议。不声明支持其他 Python 版本。

## 安装

```powershell
uv tool install --python 3.13 "git+https://github.com/source-233/minano-computer-use.git"
```

确保 uv 的工具目录在 `PATH` 中，然后检查安装：

```powershell
uv tool update-shell
minano-computer-use --version
```

这是 stdio MCP 服务，不是交互式终端应用。应由 MCP 客户端启动进程并调用工具，stdout 仅用于 MCP 协议。

### 配置 MCP 客户端

对于使用 `mcpServers` 配置的客户端：

```json
{
  "mcpServers": {
    "minano-computer-use": {
      "command": "minano-computer-use",
      "args": [],
      "env": {"PYTHONUTF8": "1"}
    }
  }
}
```

对于 OpenCode：

```json
{
  "mcp": {
    "servers": {
      "minano-computer-use": {
        "type": "local",
        "command": ["minano-computer-use"],
        "environment": {"PYTHONUTF8": "1"}
      }
    }
  }
}
```

若客户端找不到命令，将 `command` 中的可执行文件名替换为安装后的绝对路径。配置结构以对应客户端的文档为准。

## 七个工具

| 工具 | 用途 |
|---|---|
| `window` | 列举窗口，查找控件，读取树、文字和状态，执行 UIA 语义操作与条件等待 |
| `act` | 顺序执行批量动作，默认自动返回操作后的画面或文字 |
| `screenshot` | 桌面、显示器、窗口或局部截图，保存原图，定义 viewport |
| `record` | 有界录制、时间戳拼图，可选原始 PNG 帧及时间索引导出 |
| `play` | 按时间或脚本执行动作，同时录制 |
| `process` | 进程查询、启动、等待，以及独立授权的 shell 和进程终止 |
| `system` | 服务状态、取消、显示器、指针、剪贴板及 viewport 管理 |

具体参数以客户端取得的 MCP schema 为准。完整示例见 [使用指南](docs/usage.md)。

## 调用示例

以下 JSON 是对应工具的入参，不是终端命令。窗口句柄、控件名和 AutomationId 需要从实际窗口查询，不能照抄示例值。

### 查找窗口和控件

调用 `window`：

```json
{"action":"list","details":true}
```

```json
{"action":"find","target":"hwnd:0x123456","query":"搜索","compact":true}
```

目标可用明确的 HWND、PID 或唯一窗口标题；控件可用名称、AutomationId、控件类型或短期 token。多目标匹配默认拒绝，不悄悄选择第一项。

### 批量控件操作

调用 `act`，默认走严格 UIA：

```json
{
  "target":"hwnd:0x123456",
  "actions":[
    {"action":"set_value","selector":{"automation_id":"searchBox"},"value":"hello"},
    {"action":"invoke","selector":"搜索"}
  ]
}
```

支持填写、调用、勾选、选择、展开、滚动及等待。UIA 不支持某个操作时明确拒绝，**不隐式回退为真实鼠标点击**。

### 原生键鼠输入

同样调用 `act`，传入 `native=true` 即可，无需修改启动参数：

```json
{
  "target":"hwnd:0x123456",
  "native":true,
  "actions":[
    {"action":"force_focus"},
    {"action":"hold","key":"W","seconds":0.2},
    {"action":"look","dx":40,"dy":0}
  ]
}
```

原生输入会影响真实键鼠，要求目标位于前台。只有显式 `focus` / `force_focus` 才会尝试激活窗口。支持点击、双击、拖拽、多鼠标按钮、滚动、组合键、长按、文本及粘贴；批次结束会尝试释放本工具持有的输入。

### 自动反馈和图片大小

`act` 默认 `screenshot="auto"`：原生动作后返回截图，UIA 后返回有界控件文字。可用 `screenshot=false` 关闭，`true` 强制截图，`"text"` 只读取文字。

截图和整张录制拼图默认最长边 **800px、JPEG quality75**。可显式设置 `max_dim`、`format` 和 `quality`，`max_dim=0` 请求原尺寸。视频和原始帧导出保留来源分辨率，不受响应图片缩放影响。

原生坐标默认是物理屏幕像素。使用返回图片的像素坐标时，传 `coordinate_space="image"` 和对应 `capture_id`；桌面或显示器截图仍需另行指定接收输入的具体窗口。失效坐标、未知变换和越界点击会拒绝。

### 动作与录制

调用 `play`：

```json
{
  "target":"hwnd:0x123456",
  "native":true,
  "seconds":2,
  "script":"hold W 0.2\nlook 40 0\nwait 0.2\nshot"
}
```

也可传带 `at` 时间戳的 `actions`，与 `script` 二选一。`record` 只采样而不执行动作。输出包含拼图和本地 MP4 路径；编码不可用时明确返回警告，不伪造视频成功。

视频由独立 Python 工作进程调用 PyAV 编码为 H.264/MP4；取消或超时会停止并回收工作进程，不让底层编码调用永久阻塞 MCP。PyAV 绑定 FFmpeg 动态库，但不调用外部 `ffmpeg.exe`。为兼容 yuv420p，奇数宽高只在视频右侧/底部补边，不改变截图点击坐标。

## 权限与安全边界

- **UIA 默认不抢焦点**，但目标应用的 provider 仍可能自行激活窗口，不能承诺绝无副作用。
- **原生输入逐调用选择**：不传 `native=true` 就不会自动改走真实键鼠。
- **shell 与外部进程控制默认关闭**：分别需要启动参数 `--allow-shell` / `--allow-process-control`，以及单次调用的对应许可。`window.close` 也受进程控制许可约束。
- `system.cancel` 不等待长调用执行锁；取消请求被接受不等于动作已撤销或松键已完成，应检查最终结果。
- 写动作超时可能已经生效，返回不确定状态，不自动重放；批次不是事务，不提供回滚。
- 剪贴板粘贴只保留 Unicode 文本；仅当剪贴板序号未被其他应用改变时恢复，不承诺保留全部富文本或图片格式。
- 截图、视频和显式命令输出可能含敏感信息。制品保存在本地，可用 `--output-dir` 指定目录；配额仅清理本工具分配并登记的文件。
- 不安装输入驱动，不自启动，不进行后台遥测。显式授权的命令仍具有当前 Windows 用户权限，权限开关不是安全沙箱。

结果区分 `invoked`（已提交）、`verified`（后置状态已确认）、`refused`、`error`、`unknown` 和 `partial`。SendInput 或窗口消息提交成功不证明目标应用消费了输入。

## 截图后端与兼容性

默认读取屏幕合成画面，可能包含遮挡。窗口截图支持 PrintWindow 和可选 Windows Graphics Capture（WGC）；不自动激活窗口，屏幕回退须明确允许。

WGC 可在从源码安装依赖时选装：

```powershell
uv sync --python 3.13 --extra wgc
```

各后端的可用性取决于 Windows、目标应用和运行环境；结果会报告实际后端及失败原因。UIA 只适用于暴露可访问控件的应用，游戏或自绘界面通常需要原生输入。动作与采样不是硬实时，也不保证严格的动作/游戏帧配对。

## 开发与验证

```powershell
git clone https://github.com/source-233/minano-computer-use.git
cd minano-computer-use
uv sync --python 3.13
uv run ruff check .
uv run ruff format --check .
uv run pytest -q
uv run python scripts/smoke_stdio.py
uv run python scripts/verify_recording.py
uv run python scripts/verify_playback.py
uv run python scripts/verify_pyav.py
uv build
```

专项验证分别覆盖自有 Win32 控件、窗口截图、合成视频及连续帧，不把单测等同于任意应用的兼容性证明。

- [架构与模块边界](docs/architecture.md)
- [工具参数与示例](docs/usage.md)
- [能力覆盖与策略差异](docs/parity.md)
- [验证方法与证据边界](docs/verification.md)

## 许可与致谢

原创代码以 [MIT](LICENSE) 许可发布；Windows 子进程启动器的 CPython 适配部分保留 PSF 许可，详见 [第三方声明](third_party/NOTICE.md) 和 [CPython 许可证](third_party/CPython-LICENSE.txt)。

接口体验参考 [sshh12/windows-computer-use-mcp](https://github.com/sshh12/windows-computer-use-mcp)，严格控件操作思路参考 [iFurySt/open-computer-use](https://github.com/iFurySt/open-computer-use)。

视频依赖 [PyAV](https://pyav.org/) 及其 wheel 所含的编码库。依赖的许可证独立于本项目源码；重新分发这些二进制库时，应同时核对并遵守其 FFmpeg/x264 等组件的许可与声明，不能把包含 libx264 的构建一概视为纯 LGPL。
