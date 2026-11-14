# 调用例子

0.4 增加后台消息和完整应用状态，接口约束见 [v0.4.md](v0.4.md)。省略 input_mode
时仍兼容 native=false/true；显式 input_mode="uia"/"native"/"message" 选择本次通道，
不需要修改服务启动参数。

这是 MCP 工具参数，不是终端命令。名称取决于应用语言；先列窗口再读树。
工具 schema 以 MCP `list_tools` 的结果为准。

## 工具选择与参数发现

客户端收到的 `tools/list` 包含每个公开参数的说明、枚举、单位和范围，以及
actions、selector、condition、region、probe 的结构。无需为了了解输入格式
先试发动作。运行时仍进行整批预校验；schema 不是授权，也不会绕过通道限制。

| 需要 | 选择 |
| --- | --- |
| 找窗口、控件，读取 UIA／组合应用状态或做一个控件动作 | `window` |
| 顺序执行动作，最后自动观察 | `act` |
| 定时或脚本动作，同时录制 | `play` |
| 只取一张图／定义 viewport | `screenshot` |
| 不输入，只观察一段时间 | `record` |
| 查询／启动进程、等待文件或窗口、显式授权命令／终止 | `process` |
| 当前 MCP 版本与操作状态、取消、显示器／指针／剪贴板／viewport | `system` |

所有工具返回有界 JSON（`status/operation/data/error` 等），图片作为独立 MCP
image 内容返回。`invoked` 不是应用完成；部分结果、超时及 cleanup 不确定性应
独立检查，不通过重复动作来验证。参数 schema 只描述已有功能，不扩大权限。

## 0.5：事实反馈与输出迁移

原生动作默认带紧凑投递回执；需要详细的只读输入上下文时，在 act 或 play
的顶层指定 `feedback="detailed"`，省略时为 `"compact"`。它不替代 screenshot：

```json
{
  "target":"hwnd:0x123456",
  "input_mode":"native",
  "feedback":"detailed",
  "screenshot":"auto",
  "actions":[{"action":"hold","key":"W","seconds":0.2}]
}
```

示例会发送真实输入，仅应在明确允许接管且已在前台的目标上使用。反馈不会
为满足前置条件自行切窗、改输入法或更改远控设置。

逐动作回执区分本次目标、实际事件、系统接受和松键提交。实际 SCANCODE
事件中的 wVk 为 0，不能把请求解析的 VK 当成实际 wVk。text/paste 不返回
字符序列、UTF-16 单元或剪贴板内容。测得的投递间隔不是游戏实际持键时间；
系统接受 up 不等于已证明应用松键。

回执位于逐动作结果的 `receipt`：`requested_events` 是实际尝试的事件数量，
`accepted_events` 仅在全部返回可确认时为整数，有未知提交时为 null；
`unknown_events` 明示未确认数量。`phases.down/up/other` 分别保留请求、已知
接受和未知小计。`requested_seconds` 是请求时长，`dispatch_interval_seconds`
是测得投递间隔。`events` 仅含有界的非文字按键明细，`events_omitted` 标注省略。
最终字段以工具实际返回为准，不把未尝试的后续事件计作投递成功。

输出预算不足时先省略辅助输入上下文和详细事件，保留核心计数、首次失败及
清理不确定性。`accepted_events=null` 不能按零处理，也不能通过补发同一按键
来“确认”原动作是否发生。

详细输入上下文是只读的、非原子的观察，附来源和时间。HKL/ImmIsIME 只说明
布局及 IME 关联，不证明正在中文组词；本版在动作释放/清理后采集，取消或
本工具仍有持键时跳过，不是按住期间的实时状态。
无法可靠查询的 open/conversion 状态
明确 unknown。当前修饰键状态不是历史状态，也不表示该键由本工具持有。
不提供猜测性的远程拦截布尔值，agent 应结合图像和应用状态自行诊断。

修饰键观测值是 bool 或 null：查询返回零时可能是未按下，也可能是桌面/访问
限制，无法区别就返回 null，不能自动解释为“所有修饰键都已松开”。混合已知与
未知使用 partial，全部未知使用 unknown。它们不影响主动作结果或工具持键
所有权；当前上下文只有在目标进程身份与线程复核后才标注一致。

普通动作的 `target_binding` 标识本次请求解析的目标。已持有的消息按键释放
仍发送到原收件控件；这时回执里的 `recipient_hwnd`/`recipient_pid` 标识实际
接收者，method 来自实际投递记录，不因当前请求选了别的窗口而假装改用
SendInput。精简预算可能省略事件明细，不应在明细缺失时猜测收件者。

`consumption_verified=false` 仍表示“未确认消费”，而非“确认未消费”。辅助反馈
失败不会把已投递动作改成 refused；取消和本工具所有者线程的松键优先。

UIA state 拒绝的 error.details 提供静态 stage/predicate 与节点 index 等有界
信息，区分实际不匹配和 provider 读取失败；不返回节点名称、值或原始异常。
不要因下一次读取拒绝就重放已经可能生效的 invoke。

所有对外 `process_creation_time` 从 0.4 的 number 改为 0.5 的十进制字符串，
不可得时仍是 null，包括嵌套 binding 和截图元数据。MCP 的 text JSON 与
structuredContent 类型一致；JavaScript 比较时保留字符串，不经过 Number。
这仅是公开表示变化，内部 token、坐标与身份绑定仍执行精确比较。

`system(action="status")` 返回运行实例与版本信息。只有确有构建依据时才报告
构建标识，未知不填当前仓库 HEAD。磁盘包版本不能反向证明历史常驻进程版本。

## 0.4：三种输入通道

| 选择 | 行为 | 前台要求 |
|---|---|---|
| 不传 input_mode，native=false（默认）或 input_mode="uia" | 直接调用 UIA Pattern | 不主动聚焦，provider 仍可能有副作用 |
| native=true 或 input_mode="native" | 真实键鼠/原生输入 | 目标必须在前台；显式 force_focus 可请求切窗 |
| input_mode="message" | 只向绑定窗口或同进程子控件发送固定 Win32 消息 | 不要求前台，不移动真实指针，不调用聚焦 API |

显式 input_mode 即单次选择，不需要额外 native=true。将 native=true 与明确的
uia/message 模式混用会拒绝，不能逐项切换模式。默认 UIA 失败不自动改走消息或真实键鼠。

后台写入标准 Edit 的完整值：

```json
{
  "target":"hwnd:0x123456",
  "input_mode":"message",
  "actions":[
    {"action":"set_value","recipient":"hwnd:0x123457","value":"hello"}
  ]
}
```

recipient 是目标根窗口或同进程子控件 HWND。消息通道明确绑定根 HWND、PID、
进程创建时间，收件控件必须属于该根；不存在、跨进程或歧义目标拒绝。
按键/文字未给 recipient 时，仅使用目标 GUI 线程内部的焦点子控件或根窗口，
不借用全局前台控件。文本经 WM_CHAR 投递；set_value 仅在已支持的标准输入框上
执行完整值替换并回读，不等同 UIA SetValue。

后台鼠标仍要给出目标窗口内的物理屏幕坐标，或使用同调用者最新有效 capture_id
映射 image 坐标。它在目标窗口内部选择接收控件并换算客户区坐标，不使用真实
鼠标命中，也不穿透到其他进程。move/click/drag/wheel 的消息不是系统真实输入；
目标应用可能忽略、重解释或自行聚焦，返回 invoked 不保证应用接受。
本工具的后台有效载荷统一使用有时限的同步消息投递，避免异步文字在后续完整值
替换之后才处理；窗口过程返回仍不等于应用业务完成，不承诺系统消息队列原子性。
相对 look、force_focus、paste 不属于后台消息动作，整批在执行前拒绝。

消息通道默认后置反馈是同窗口的 UIA 状态与可选截图。反馈不会重新发现同名窗口，
也不重试写操作。取消/超时后可能已有消息入队；工作进程被硬终止时不能声称其
按键松开 finally 已执行，结果会保留不确定性和未确认清理。

## 0.4：完整应用状态与控件信息

```json
{
  "action":"state",
  "target":"hwnd:0x123456",
  "include_ax":true,
  "include_screenshot":true,
  "capture_mode":"window",
  "allow_fallback":false,
  "include_tokens":true,
  "rich":true,
  "text_limit":500,
  "depth":8,
  "max_nodes":500,
  "max_dim":800,
  "format":"jpeg",
  "quality":75
}
```

设置 `include_tokens=true` 时，已观察到且可安全绑定的控件节点会带有短期
`token` 和 `token_ttl_seconds`，可以直接传给后续的 `act` 或 `window` 控件动作；
不需要再 `find` 一次。token 绑定具体窗口、进程和 UIA 元素，默认 TTL 为 60 秒，
最多缓存 256 个；树被截断时只会给已观察到的节点发 token，不代表未返回节点不存在。
过期、重启 worker、窗口或元素身份变化会拒绝 token，必须重新观察，不能按名称自动重搜。

返回的 data.uia 包含控件、目标内焦点与选区；data.screenshot 是图像元数据，
图片仍是独立 MCP image 内容。state 不主动切到前台。两个组件绑定相同根窗口
身份，并有各自时间及 component_errors；consistency="non_atomic" 明确不是同帧。
一方失败仍保留另一方，整体报告 partial，而不是全部成功。
`capture_mode`/`allow_fallback` 使用独立 screenshot 相同的已有后端与回退策略；
省略仍为 screen/false。示例要求窗口自身后端，取图失败不会自动激活或改用
桌面截图。根 binding 真失效时两个分量仍全部丢弃。

关闭 include_ax 可完全跳过 UIA，关闭 include_screenshot 不取图；二者不能都关。
全局前台窗口和目标内 UIA 焦点是不同概念，焦点不属于目标时不会泄漏别的应用内容。
选区只从已证明属于目标的控件读出；未知、无焦点或不支持时明确标注 unavailable。

宿主 HWND 的进程与 UIA 控件进程可以不同。跨进程控件需要实时 UIA 祖先链到达
同一根，且该路径上存在属于控件进程的原生 HWND，并被 Win32 确认为宿主的子窗口。
分别校验宿主和控件进程创建时间；token 保存原锚点／归属，使用时不得换绑。
控件可没有 HWND，但祖先必须提供同进程锚点。无锚点、身份不可读、链断开、根进程
不等于目标宿主等情形仍拒绝；不按进程名豁免，不表示所有 UWP 拓扑都支持。

tree/text/find/inspect 可指定 rich=true，查看类名、原生 HWND、启用/离屏/焦点状态、
矩形、当前值和 actions 提示。每个可选属性的读取失败分别报告，不用 false 冒充
不可读取。provider 的屏幕坐标不自动等于可安全点击的物理坐标；几何信息不启用
物理回退。密码值、密码文字和密码选区不明文返回；无法判断敏感性时保守省略。

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

`set_value` 只提交一次写入，然后在同一已绑定 UIA 元素上做短时只读确认，最长约
600ms、约每 50ms 轮询一次，并受调用剩余期限约束。身份或元素引用变旧会立即停止；
超时/读回不匹配返回不确定，不会重新查找或重写。重试前先重新观察当前值。
确认期限耗尽后不再发起新的 UIA 查询；无法完成最终隐私校验时，`before` 和
`after` 均省略为 null，并标记 `value_redacted=true`。已在执行中的 COM 调用仍由
worker 的硬超时与取消回收机制约束，600ms 不是对任意 provider 响应时间的保证。

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

`actions` 与 `script` 必须二选一，展开后最多 100 个动作。`seconds` 是本次录制
时长，范围 **0.1..30 秒**，不是每个动作的持续时间。`at` 是相对本次调用开始的
秒数，范围 0..60，且必须 `at <= seconds`，所以当前实际上不能把动作排到第
30 秒之后，更不能传 `at=62`。每个动作内部的 `seconds` 是该按键／拖拽等的
持续时间；动作串行执行，较长动作可能使后续动作晚于计划。

原生输入失去前台时停止／拒绝后续事件并尝试松键，不自动重新聚焦或重放。
`TARGET_NOT_FOUND` 只代表目标窗口不可用，不证明进程已退出。需要确认进程退出
时另用 `process` 查询／绑定等待。`timeout` 不是所有原生动作的硬总时限。
从 `data.actions.data` 读取逐动作结果、计数与 cleanup，从 `data.capture` 单独
检查录像结果；录像失败不等于之前的动作全部失败。

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

### 矩形格式与坐标空间

- `window.list` 的 `rect` 配有 `rect_format="left_top_right_bottom"`，右／下边界不含。
  宽高应计算 `right-left`、`bottom-top`，不要把后两项当作宽高。
- `rect_space` 根据读取线程的实际 DPI awareness 标为 `physical_screen`、
  `dpi_virtualized_screen`、`system_dpi_screen`、`win32_screen_unknown` 或 `unavailable`。
  不改变调用方 DPI 上下文，不把未知空间冒称 DIP 或物理像素。
- 截图的 `window_rect_format/window_rect_space` 单独标识窗口边界，图像范围另用
  `surface_bounds=[0,0,width,height]`、`surface_bounds_space="surface_pixels"`。
  `region_result_space="surface_pixels"` 说明返回的 region 已换成原始图像内坐标。
- `scale_from="cropped_surface_pixels"`、`scale_to="output_pixels"` 表示 response 缩放；
  montage 则从 `montage_pixels` 缩放。`scale` 不适用于窗口矩形／DPI 换算。
- `physical_transform` 是独立的已知物理映射。明确为虚拟化／未知空间的窗口截图，
  不发布猜测性的映射；未知即 null。多显示器 DPI 换算不能只乘 `dpi/96`。

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

缺少任一层许可返回 `refused / PERMISSION_REQUIRED`，message 分别指出缺少的
`startup option --allow-shell/--allow-process-control` 和／或单次
`allow_shell=true/allow_process_control=true`。单次传 true 不会绕过启动门禁。
`close` 仍发送优雅 WM_CLOSE，但可能丢失数据，保留与 kill 相同的双重许可；
这项修复只改善权限诊断，没有降低关闭／终止的授权要求。
shell 子进程先暂停创建、放入 Job Object，再运行；取消/超时清理其子树。
普通 launch 的应用不会在 MCP 退出时被当作工作进程杀掉。
进程名多匹配拒绝，批量终止必须显式 all=true；不允许终止服务及其活动 worker。
门禁不是安全沙箱：获准的程序或命令仍具有当前 Windows 用户的权限。
