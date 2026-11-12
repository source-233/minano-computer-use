# 调用例子

这是 MCP 工具参数，不是终端命令。名称取决于应用语言；先列窗口再读树。
工具 schema 以 MCP `list_tools` 的结果为准。

## 窗口与控件

`window` 列窗口：

```json
{"action":"list"}
```

明确 HWND（示例需换成实际值）后读树：

```json
{"action":"tree","target":"hwnd:0x123456","depth":8,"max_nodes":500}
```

`text` 单独读取文字，`inspect` 查看控件状态和 patterns。
标题先不区分大小写精确匹配，再查唯一子串；PID/标题多窗口会拒绝。

按 UIA Name 严格调用按钮，不回退真实鼠标：

```json
{"action":"invoke","target":"hwnd:0x123456","selector":"Apply"}
```

填写并精确回读：

```json
{
  "action":"set_value",
  "target":"hwnd:0x123456",
  "selector":{"automation_id":"1001","control_type":"Edit"},
  "value":"你好 Minano"
}
```

字符串 selector 是区分大小写的精确 Name；对象条件 AND 组合。可明确传零起点
`index` 选重复项；搜索被截断时仍拒绝。普通按钮仅返回 `invoked`，不虚报完成。

## 批量控件操作

`act`：

```json
{
  "target":"hwnd:0x123456",
  "actions":[
    {"action":"set_value","selector":{"automation_id":"1001"},"value":"hello"},
    {"action":"invoke","selector":"Apply"},
    {"action":"inspect","selector":{"automation_id":"1001"}}
  ]
}
```

最多 100 项，默认遇错停止，保留逐项结果；不是事务，不会回滚。
`unknown` 可能已生效，不自动重跑。`wait` 是有限秒数等待，不是条件等待。
UIA scroll 的 `direction` 是 up/down/left/right，`amount` 是 small/large，
不支持 ScrollPattern 就拒绝。

`act` 默认 `screenshot="auto"`，不用每次再单独调截图：
- `native=true`：批次后自动返回最长边800的 JPEG，包含新 capture_id。
- 默认 UIA：批次后自动返回有界控件文字/状态，不生成不必要的图片。
- `screenshot=false`：关闭自动反馈；`true`：强制图片；`"text"`：只回读文字。

反馈位于 `data.after`，图片作为独立 MCP image 返回；动作结果仍逐项保留。
自动反馈不主动聚焦，失败只报告观察失败，不重复点击、不改写此前不确定结果。
取消后不继续截图；画面有变化也不能自动等价为任务已经成功。

## 显式原生输入与录制

`native=true` 直接选择本次调用的原生键鼠，不需要启动参数或重新连接。
`native=false`（默认）仍是严格 UIA。参数作用于整个批次，不在单项里启用。
目标需要已在前台；只有明确的 `focus` / `force_focus` 动作会尝试激活窗口。

`act` 示例：

```json
{
  "target":"hwnd:0x123456",
  "native":true,
  "actions":[
    {"action":"hold","key":"W","seconds":0.2},
    {"action":"look","dx":40,"dy":0}
  ]
}
```

`play` 使用非递减 `at` 秒数，独立采样画面：

```json
{
  "target":"hwnd:0x123456",
  "native":true,
  "seconds":2,
  "fps":10,
  "actions":[
    {"at":0.2,"action":"hold","key":"W","seconds":0.2},
    {"at":0.8,"action":"look","dx":40,"dy":0}
  ]
}
```

晚于计划的动作立即执行，不倒退时间；不是硬实时。批次结束尝试释放持键。
采样时间戳不证明游戏收到输入，也不证明动作/帧严格配对。
截图和录制是屏幕合成画面，可能包含遮挡，不主动聚焦窗口。

## 显式 force_focus

如果确实需要切到前台，可以在 `act(native=true)` 中显式使用：

```json
{
  "target":"hwnd:0x123456",
  "native":true,
  "actions":[{"action":"force_focus"}]
}
```

它会按 sshh12 的方式临时连接当前线程、当前前台窗口线程和目标窗口线程，
恢复最小化窗口，执行 `BringWindowToTop`/`ShowWindow`/`SetForegroundWindow`，
然后重新核对目标 HWND 是否确实成为前台，并在 finally 中解除线程连接。

`force_focus` 不会自动插入到后续键鼠动作前；必须由调用方显式调用。Windows 仍可能
拒绝前台切换，失败会返回 `FOCUS_NOT_VERIFIED`，不会因此继续发送鼠标或按键。
普通 `focus` 保持原来的保守 `SetForegroundWindow` 行为，UIA 操作也不会调用它。

## 0.2：定向查找、等待、票据

`window` 定向查找（返回发现结果，不放宽写操作的精确唯一规则）：

```json
{"action":"find","target":"计算器","query":"等于","match":"contains","limit":10,"compact":true}
```

find/inspect 可返回 token，用于后续短期绑定目标。token 和 selector 二选一：

```json
{"action":"inspect","target":"hwnd:0x123456","token":"上次返回的完整token"}
```

token 只在原 UIA worker 内有效，TTL60 秒、缓存256项；控件变更、过期或 worker
重建后要明确重新发现，不会偷偷用名称搜另一个。provider 身份能力有限，
这不是对所有可能句柄复用的绝对证明。

在单次操作之后等待明确状态，不重复操作：

```json
{
  "target":"hwnd:0x123456",
  "actions":[
    {"action":"invoke","selector":"Apply"},
    {"action":"wait_for","selector":"Ready","condition":{"exists":true},"timeout":2}
  ]
}
```

条件只选一项：exists、value、toggle_state、selected、expand_state。
超时返回最后观察；provider 错误、搜索截断和失效票据不当作控件消失。

window/act 支持 max_output_chars 和 max_output_bytes；默认24000/65536，
计算 JSON 和 MCP 文本/结构化双份封装。输出被删减会标 output_truncated；
动作状态、错误码、不确定性会优先保留，不把显示截断当作搜索唯一性。

## 0.2：取消、ROI、轨迹

长动作执行期间，可以独立调用 `system`：

```json
{"action":"cancel"}
```

取消无需等待主执行锁；响应只是请求已发出，最终批次结果才报告停止和松键情况。
取消后不执行后续动作，即使 stop_on_error=false。UIA 卡住时回收工作进程；
可能已提交的写动作仍标 unknown，不自动重放。原生松键失败会阻止下一次原生操作，
直到释放成功。崩溃、断电、进程强杀不是协作取消可以保证的范围。

`screenshot`：

```json
{
  "target":"hwnd:0x123456",
  "capture_mode":"window",
  "region":[10,10,210,110],
  "max_dim":800,
  "allow_fallback":false,
  "timeout":5
}
```

region 是选定 surface 原始像素内 [left,top,right,bottom]，先裁剪后缩放。
capture_mode 可选 screen（默认）、window（PrintWindow 后 WGC）、printwindow、wgc。
只有明确 allow_fallback=true 才可回退屏幕区域；实际后端/失败原因会反馈。
WGC item 的像素不能无证据等同 GetWindowRect 物理屏幕坐标；变换不明确就返回未知。
不因纯黑图就武断断言失败，也不把超时包装成有效图片。

可用启动参数开启脱敏轨迹与配额：

```powershell
uv run minano-computer-use --trace --artifact-max-mb 256 --artifact-max-files 200
```

日志默认不记录输入文本、控件名、窗口标题、异常消息和截图内容。配额只清理
本工具分配并登记的 artifact，不扫描删除用户目录；审计不能自动重放动作。
同一调用返回的 montage、MP4 和 trace 作为一组保留，直到下一次产制品调用
才可按配额淘汰。若当前结果组本身超过配额，报告 over_budget 而不返回已被
删除的路径；单文件超过字节上限仍拒绝。写入通过安全原子替换，不重新打开
可能被替换成链接的 reservation；编码工作进程不直接写入对外 artifact 路径。

## 0.3：单次切换与完整鼠标

原生输入只需本次传 `native=true`。历史 `--allow-native` 参数保留为兼容项，
不再控制权限；也不改变默认的严格 UIA。

```json
{
  "target":"hwnd:0x123456",
  "native":true,
  "actions":[
    {"action":"force_focus"},
    {"action":"right_click","coordinate":[500,300]},
    {"action":"double_click","x":500,"y":300},
    {"action":"drag","x":500,"y":300,"to":{"x":650,"y":400},"duration":0.5},
    {"action":"scroll","direction":"down","amount":2}
  ]
}
```

示例坐标仅用于说明，不能照抄到真实桌面。默认 `coordinate_space="screen"` 是
物理屏幕像素。`click` 可指定 left/right/middle/x1/x2、clicks=1..3、modifiers；
`mouse_down/mouse_up` 支持按住鼠标，批次结束仍会清理。拖动 duration 是全程时长，
不是每一段时长；`to` 和 `path` 二选一。`scroll` 单位是滚轮格数，
`wheel.amount/delta` 仍是原始滚轮 delta（通常一格120），两者不要混淆。

`key_method` 可选 scancode（默认）、message、auto。message/auto 也要求
`native=true` 和正确前台；浏览器渲染子窗口不唯一时拒绝，不向所有子窗口广播。
消息提交成功只报告 invoked，不等于网页/游戏实际消费。`text` 默认把换行和 Tab
作为对应按键，可用 `literal=true` 保留 Unicode 文本行为。

## 0.3：截图坐标和 viewport

截图目标增加 `display:0`、`display:primary/left/right`、`foreground`、
`window:process:app.exe`、`viewport:名字`。窗口/进程匹配仍需唯一。
`max_dim=0` 返回全尺寸；`save=true` 另存未缩小的 PNG 到配额内制品。
普通截图及录制拼图默认最长边800、JPEG quality75；可显式指定 max_dim、
format（png/jpeg/webp）、quality。拼图限制的是整张图片，不是每个格子。
优先控件文字和局部截图；读小字时再提高分辨率。图片压缩体积不等于视觉 token。

```json
{"target":"hwnd:0x123456","capture_mode":"screen","max_dim":1200,"define_viewport":"app"}
```

读取返回的 `capture_id` 后，使用该截图的像素坐标：

```json
{
  "target":"hwnd:0x123456",
  "native":true,
  "coordinate_space":"image",
  "capture_id":"上次截图返回的完整ID",
  "actions":[{"action":"click","x":200,"y":100}]
}
```

窗口身份/位置/大小/DPI 变化、未知物理变换、越界或截图失效都会拒绝，不裁剪到边界、
不悄悄重截再执行。当前截图上下文按客户端连接及 OpenCode 会话隔离；
record/play 拼图不会替换可点击截图。相对 look 的 dx/dy 不按截图缩放。
桌面/显示器截图只提供坐标来源；点击时仍要明确 `target="hwnd:..."` 作为接收窗口。
不能把 desktop/display 当作原生键鼠接收对象，也不会擅自选择当前前台窗口。

viewport 绑定原窗口身份，移动/缩放时按相对区域追踪，不能按标题认领别的窗口。
用 `system` 的 `viewports` 列举、`clear_viewport` 配合 `name` 或旧 `text` 删除。
屏幕 viewport 不是键鼠接收窗口；执行输入还要指定具体窗口。

## 0.3：动作脚本、录制

`play` 的 `actions` 和 `script` 二选一。脚本每行一个命令，整行 `#` 是注释：

```json
{
  "target":"hwnd:0x123456",
  "native":true,
  "seconds":3,
  "script":"hold W 0.2\nlook 40 0\nwait 0.1\nlook -40 0\nshot"
}
```

还支持 tap/down/up/move/lmb/rmb/mmb/scroll/type/paste/probe/until。
脚本完整解析校验后才执行；无 eval、未知命令拒绝。`until` 是对最近一次 probe
结果的单次判断，满足则结束脚本，不是循环探测。probe 是有权限门禁的 shell，
返回有界 JSON 对象；没有 shell 启动和调用授权时，整段脚本执行前即拒绝。
录制与动作有独立时间戳，不保证严格配对；脚本结束后停止采样。

`record` 增加区域、viewport、`keep_frames=true`（导出帧及时间索引），并返回
achieved_fps、静帧提示和编码诊断。H.264 为奇数宽高补右/下边缘，logical_size
与 encoded_size 分开报告；不会借补边改变点击坐标。
MP4 由 PyAV 在独立 Python 工作进程内生成，不依赖 ffmpeg/ffprobe 命令。
编码取消/超时会回收该进程；编码失败仍可返回拼图，但 mp4_path 为空并附诊断，
不会用不存在或不完整的视频路径冒充成功。原始帧导出仍由 keep_frames 显式选择。

## 0.3：剪贴板、OCR、进程

`system` 支持 cursor/get_clipboard/set_clipboard。显式 set_clipboard 传 text，
会精确回读；默认轨迹不记录内容。原生 paste 临时写入 Unicode 文本并发送粘贴，
只有剪贴板序号仍属于本次写入才恢复，避免覆盖用户或应用的新内容。
恢复失败单独报告；不承诺保留剪贴板的图片/富文本等全部格式。

`window.get_text` 先读 UIA，只在成功且文字为空时用本机 WinRT OCR；
UIA 超时/截断/错误不伪装成空。OCR 不需要任意 shell 权限，也不执行用户源码。

`process` 支持 list、launch（argv 或 exe/args/cwd）、wait、wait_for_file、
wait_for_window、shell、kill。launch 的 shell=true 是 OS 打开文档/URL/URI，
不是任意命令解释器；命令解释器必须走受门禁的 shell 动作。

高风险能力默认关闭，单独按需启动：

```powershell
uv run minano-computer-use --allow-shell --allow-process-control
```

调用 shell 仍要传 `allow_shell=true`；kill 或 window.close 仍要传
`allow_process_control=true`。这两个门禁与便捷的 native 开关独立。
shell 子进程先暂停创建、放入 Job Object，再运行；取消/超时清理其子树。
普通 launch 的应用不会在 MCP 退出时被当作工作进程杀掉。
进程名多匹配拒绝，批量终止必须显式 all=true；不允许终止服务及其活动 worker。
门禁不是安全沙箱：获准的程序或命令仍具有当前 Windows 用户的权限。
