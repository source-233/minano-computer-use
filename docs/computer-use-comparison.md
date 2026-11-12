# 主流 computer-use 实现对照与取舍

核查日期：2026-11-11。对照的是固定提交的源码和文档，
不是星数、演示效果或本机兼容性实测。此次没有安装这些项目、添加依赖、
操作用户桌面或变更现有 MCP。基线仍为本项目 0.1.0。

**后续状态（2026-11-12）：** 用户已授权扩展，本文的候选已进入 0.2 实现，
接口见 [扩展契约](expansion.md)，最终实测证据见 [验证说明](verification.md)。
本文保留原研究结论，不把候选列表当成“全部跨环境验证成功”的保证。
WGC 本机仍报捕获项转换失败；PrintWindow 是已取得真实窗口图像的路径。

## 结论

**不需要整套复刻任何一个项目。保留七工具和严格 UIA 执行器定位，
补可靠性与调用效率，而不是变成模型规划器、云平台或任意代码执行器。**

已经具备的能力不算新需求：按名称/AutomationId 的精确唯一选择、
批量动作、文字/截图分离、截图 origin/scale、有时间戳的录制、
UIA 子进程隔离和超时回收、写操作不重放、invoked/verified/unknown 区分、
原生输入双门禁及失败清理。相关证据见 [验证说明](verification.md)。

## 参考范围和固定提交

| 项目 | 本次固定提交 | 所属层，不应混淆 |
|---|---|---|
| Anthropic computer-use-demo | `4a4224bdf0e9cfb3332167b69c90457403813625` | Linux/X11 执行器和模型循环，不是 Windows UIA 服务器 |
| OpenAI computer-use sample apps | `6dd34b40acc1d002696ccd41aaf0a1f2f2be290c` | Playwright/PyAutoGUI agent runtime 和示例控制台，不是生产安全边界 |
| Microsoft Playwright MCP | `e436f64eb7e1904b6c302da0997a2243496d191e` | 浏览器专用 MCP；不能把 DOM 引用直接当 Windows identity |
| Cua Driver | `f9a2768c3d7eda97dc9a6e462415759e84d8a61e` | 原生桌面 driver；可借鉴执行契约，不引入整个云/沙箱产品 |
| Microsoft UFO | `eae3c0a8d168c47cb10df0652e293e092eda1078` | 完整 Windows agent/编排系统，不宜嵌入本项目 MCP 内核 |

## 最值得补的能力

### P1 — 定向查找 + 总输出预算

**直接提升用户认为“顺滑”的调用体验，优先于增加工具数量。**

Playwright 的 `browser_find` 返回匹配节点、路径和少量上下文，
避免为了找一个按钮反复返回完整 accessibility snapshot。[P1]
本地已有深度/节点预算，但单节点文字最多 4096 字符不等于总响应预算：
500 节点仍可能产生约 200 万字符，Name 等字段也没有总量限制。

建议在现有 `window` 下增加只读 `find` 和 compact 返回模式，
统一总字符/字节预算、明确截断原因和匹配总数是否已知。
不要用展示截断后的集合声称写操作选择唯一；读搜索可以方便地用子串，
写选择仍必须精确、唯一、重新验证。

验收：大树/超长文本输出有硬预算；重复项和截断搜索不触发任何写动作；
定位一个目标不要求回传整棵树。

### P1 — 条件等待和有界后置验证

本地 `wait` 目前只有固定秒数；状态变更后立即回读，
对异步 UI 可能过早返回 unknown。应等待目标状态，而不是一律 sleep。

建议增加 `wait_for`（仍是 `window`/`act` 的 action），可等待元素存在/消失、
value、toggle/selection/expand 状态；明确总 deadline 和 poll interval。
操作只执行一次，之后仅重读状态；超时返回最后观察，不重复 Invoke/Toggle。
不能让每次重启 worker 或每项轮询重置总期限。

验收：延迟生效的 fixture 能被验证；永不生效按总期限退出；
记录调用次数证明写动作没有因轮询或超时重复。

Cua 的 `post_action_wait` 是可借鉴的契约思想，但本次固定源码明确：
约 300ms 的窗口变化观察及 `none` 跳过参数是 **macOS adapter** 的实现，
Windows 不在动作后观察窗口变化，也不接受该参数。不能把共享模块里存在该功能
误写成 Windows 已有能力。[C3] 本项目的 Windows 条件等待仍需独立实现和验证，
不应给所有动作盲目增加固定延迟。

### P1 — 原生动作可独立取消与紧急松键

现有 foreground 检查和 finally 松键是基础，但不是完整接管机制。
当前 runtime 串行锁意味着不能靠再调用一个同样排队的工具及时中断长批次。
这对严格 UIA 日常调用不是必需，对长期启用原生输入则必须先补。

建议独立于长调用的取消信号，hold/wait/定时调度都检查；
只释放本服务登记的输入，不恢复/覆盖用户焦点，不自动重跑半完成批次。
服务断连和异常退出的清理边界需要单独证明，不能把 try/finally 当崩溃保证。

验收：synthetic adapter 可在持键和等待期间取消；停止后没有新动作，
清理结果明确；取消通路不依赖被阻塞的 UIA worker 或 runtime 锁。

### P2 — 强目标绑定和失效拒绝

当前每次重新解析目标、验证 UIA root PID 并拒绝歧义，
比缓存裸元素编号稳妥。但还没有跨调用的窗口身份票据，
也没有防止窗口重建/句柄复用的 generation。

如果以后为了节省调用加入短 ref，应同时绑定 PID、HWND、进程/窗口代次、
worker generation，并在动作前重新验证；失效只能拒绝，不能悄悄定位同名新目标。
不跨进程保留 COM 对象，也不要仅因为有 capture_id 就声称画面新鲜。
参考 Cua 的 snapshot/index/PID 绑定与 `stale_element_token` 明确拒绝，
而不是把裸序号直接传给下一次动作。[C2]

验收：关闭重开窗口、PID/HWND 不匹配、worker 重建、旧 ref 使用均明确拒绝。
如果仍只使用 fresh selectors，不必为模仿别人的 API 强行先做 refs。

### P2 — 可选调用轨迹与磁盘保留预算

OpenAI 样例保留 workspace、截图、Replay JSON 和截图时间线，
并明确循环正常结束不是任务成功证明。[O1]
本地已有录制文件，但没有把工具调用、结果和 worker 回收串起来的统一 manifest，
也没有跨调用 artifact 磁盘配额。

建议显式开启本地 run_id/call_id + JSONL manifest，关联时间、结果状态、
may_have_acted、脱敏目标标识和 artifact 路径；默认不记录输入文字、剪贴板或整棵树。
容量/时间配额和删除入口必须同时存在，日志不能写 MCP stdout。
审计不等于自动重放，时间线也不是严格动作/帧配对证明。

验收：故障能关联到具体调用；敏感内容默认不落盘；配额只清理本项目拥有的目录。

### 按需求再做 — ROI/zoom 与截图后端

Anthropic 的 zoom 会映射到物理分辨率截图再裁剪，
而不是把已经缩小的图片简单放大；坐标语义也明确不因为 zoom 改变。[A1]
本地已有 origin 和实际双轴 scale，缺少 explicit coordinate_space、
原始尺寸/frame ID 和有界 ROI。建议先补契约与混合 DPI/负屏幕原点测试。

后台窗口截图若出现实际遮挡需求，再评估 PrintWindow/WGC。
引入后必须报告实际后端、是否 screen-region fallback、遮挡/失败状态，
不能为了得到一张图自动拉前窗口，也不能把黑图或桌面裁剪冒充后台捕获。
本次审计的 Cua Windows capture 路径提供了 PrintWindow/WGC/屏幕区域的
分层参考；这些只证明存在相应实现，不证明本机应用一定兼容。[C4]

### 低成本配套 — 更明确的 delivery 与 MCP 风险提示

已有 method/status，不应重造第二套成功语义。可逐步补充 delivery、
PID/HWND binding 和 fallback 列表（严格 UIA 应为空），方便诊断具体执行路径。[C5]
确定只读的 MCP 工具可加 readOnlyHint，混合读写的 window/act 保守标注；
这些 hints 仅给宿主决策参考，不是授权、审批或安全隔离。

## 不建议复制

- **OpenAI/Anthropic 的完整模型循环**：调用模型、推理、规划由 OpenCode 等宿主负责。
  示例没有生产级 OS sandbox/action review，不能搬来当安全保证。[O1]
- **任意 Python/JS/shell 执行工具**：它会扩大本地授权面，和本项目的严格动作协议相反。
- **UFO 的隐式激活和物理回退**：其 controller 构造会 set_focus，
  一些路径执行 pyautogui；这不符合“不打扰前台”的默认约束。[U1]
- **完整云服务、VM、RAG、跨设备多 agent**：属于产品另一层，当前没有需求和验证路径。
- **默认视觉模型、OCR/OmniParser 大依赖**：先靠 UIA 和明确截图解决，
  真有无 UIA 场景再按选装插件和准确许可证评估。
- **把风险 annotations 当审批/安全隔离**：可补 MCP hints，但混合读写工具必须保守标注，
  不能用一个 confirmed=true 宣称防提示注入。

## 建议的下一步

1. 先让用户在不替换旧 MCP 的情况下试用严格控件核心。
2. 第一轮只补 `find/compact` 和总输出预算。
3. 根据真实慢响应样本补条件等待；原生输入扩大使用前补独立取消。
4. trace、目标票据、ROI/后台捕获按具体问题追加，不全部一次实现。

这是评估/候选顺序，**不是已经实现的功能列表**。本次仅新增比较文档，
不改变 0.1.0 的行为和全局安装。

## 固定来源

- [P1: Playwright MCP README, browser_find/output/session](https://github.com/microsoft/playwright-mcp/blob/e436f64eb7e1904b6c302da0997a2243496d191e/README.md)
- [O1: OpenAI sample README, artifacts and safety limits](https://github.com/openai/openai-cua-sample-app/blob/6dd34b40acc1d002696ccd41aaf0a1f2f2be290c/README.md)
- [A1: Anthropic computer.py, zoom and coordinate mapping](https://github.com/anthropics/anthropic-quickstarts/blob/4a4224bdf0e9cfb3332167b69c90457403813625/computer-use-demo/computer_use_demo/tools/computer.py)
- [C1: Cua Driver README](https://github.com/trycua/cua/blob/f9a2768c3d7eda97dc9a6e462415759e84d8a61e/libs/cua-driver/README.md)
- [C2: Cua element_token, stale refusal](https://github.com/trycua/cua/blob/f9a2768c3d7eda97dc9a6e462415759e84d8a61e/libs/cua-driver/rust/crates/cua-driver-core/src/element_token.rs)
- [C3: Cua post_action_wait, macOS-only skip and explicit Windows exclusion](https://github.com/trycua/cua/blob/f9a2768c3d7eda97dc9a6e462415759e84d8a61e/libs/cua-driver/rust/crates/cua-driver-core/src/post_action_wait.rs)
- [C4: Cua Windows capture](https://github.com/trycua/cua/blob/f9a2768c3d7eda97dc9a6e462415759e84d8a61e/libs/cua-driver/rust/crates/platform-windows/src/capture.rs)
- [C5: Cua Windows click delivery](https://github.com/trycua/cua/blob/f9a2768c3d7eda97dc9a6e462415759e84d8a61e/libs/cua-driver/rust/crates/platform-windows/src/tools/click.rs)
- [U1: UFO UI controller](https://github.com/microsoft/UFO/blob/eae3c0a8d168c47cb10df0652e293e092eda1078/ufo/automator/ui_control/controller.py)
