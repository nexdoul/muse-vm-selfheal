# M5：无需读取目标进程 cwd 的身份校验

`adapters/approver.py` 已删除 identity 对 `/proc/<pid>/cwd` 的读取，也不读取 `exe`、`environ` 或 `fd`。权限不足沿调用链暴露为退出 2；`bin/health.sh` 将它报告为 `status=FAILED, reason=environment`。正常进程消失仍返回 None，服务缺失或业务日志失效仍退出 1。

## 新身份规则

共同条件保持：`/proc/<pid>` 所有者 UID 等于当前 UID、argv[0] basename 等于 NODE_BIN basename、排除 `--check`、排除 stat 中 Z 状态、取得 stat 第 22 字段 starttime。发 TERM 前仍打开 pidfd，重新校验身份并比较 starttime token；校验失败不发信号，当轮 TERM 后不启动替代进程。

脚本位置按两种形式校验：

- **绝对路径**：解析后必须严格等于 `APPROVER_ROOT/muse-daemon.cjs`。同名异目录、其他脚本均拒绝。这一路不增加 pidfile 前提，原来的缺失或陈旧 pidfile 用例仍能发现真实进程，防止重复启动。
- **相对路径**：只接受 `muse-daemon.cjs`（Path 规范化后也接受 `./muse-daemon.cjs`），拒绝 `other/muse-daemon.cjs`、`../muse-daemon.cjs` 和相似名称。再读取目标 root 下 `data/muse-daemon.pid`，要求 PID 相等，并用 `/proc/stat` 的 btime、starttime 和 SC_CLK_TCK 计算启动时间，要求 `启动时间 <= pidfile mtime <= 当前时间`。内容与 mtime 从同一个打开的文件描述符取得。

相对路径候选的 pidfile 缺失、格式错误、PID 不匹配或时间不符，表示无法建立目录归属，抛 IdentityUnavailable 并退出 2；不能当作未发现进程后启动新副本，也不能据此发送 TERM。这里没有“读不到 cwd 就跳过校验”的分支，没有只凭 basename 就授权。

后续由 repair 启动的 precheck 与常驻进程均使用脚本绝对路径，cwd 仍设为 root。这样新实例无需依赖 pidfile 来证明脚本位置；现存相对路径实例使用上述兼容规则。

**证据边界**：pidfile 是服务自身维护的可信本地文件，不是不可伪造的身份凭证；mtime 检查只能排除早于当前启动时间或未来的证据，不能抵抗同 UID 主动伪造 PID/argv/mtime。btime 为整数秒，启动时间换算存在不足一秒的精度边界，并受系统时钟调整影响；发送信号时的代次校验仍使用原始 starttime token 与 pidfd，不用 mtime 代替。此实现不验证 Node 二进制本身，保持原来的 argv basename 契约。

## cwd 契约与 M4 用例的明确变化

M4 的 `test_identity_cwd` 原本要求：目标脚本绝对路径正确，但 cwd 不同，必须拒绝。M5 明确移除 cwd 依赖后，无法用允许读取的 argv/stat/UID 证明这个 cwd 条件。因此该用例保留，但改为证明绝对脚本身份不受 cwd 影响；相应反证改为“恢复 cwd 依赖”必须失败。不能把本次结果表述为旧 cwd 断言原封不动通过。

其余 22 个 M4 用例的业务断言保留，完整 23 个用例均通过。新增 M5 用例覆盖相对脚本的目录归属证据。夹具统一使用唯一 NODE_BIN basename，仍创建真实进程并读取真实 /proc；首次验证发现本机已有同 UID、相对路径启动的同名 daemon，原先固定 `node` 的夹具会碰到这些进程。唯一名字隔离测试与真实服务，未改生产匹配逻辑，未停止这些已有进程。

生产中若同时存在另一个相同 UID、相同 NODE_BIN basename、相对脚本名也相同但无法绑定到目标 pidfile 的实例，扫描会退出 2。它无法证明这个候选属于哪个目录，故不会自动忽略它。这种部署需要由部署所有者明确实例归属或采用绝对脚本启动方式。

## 错误码与能力探测

| 情况 | adapter | 清单结果 |
|---|---|---|
| 身份成立且末条业务日志新鲜 | 0 | READY/ready |
| 进程退出、身份明确不匹配、日志缺失/过期/非法 | 1 | FAILED/nonzero |
| 必需字段 PermissionError、其他传播出来的 OS 错误、相对身份证据不足 | 2 | FAILED/environment |
| stop 标记存在 | check=1，repair=0 | 仍由 stopped_check 证明 STOPPED |

`identity()` 显式重新抛出 PermissionError；FileNotFoundError/ProcessLookupError 表示进程读取竞争中的消失，返回 None；损坏的 stat 数据报告环境错误。`processes()` 不把权限异常转换为空列表。main 捕获 OS 错误和 IdentityUnavailable，stderr 输出带路径的 `approver: environment: ...`，返回 2。pidfile、日志读取以及 pidfd 打开/发送信号的权限异常也不会变成服务缺失。

main 开始对自身 cmdline/stat 做能力探测，然后尝试自身 cwd 的 readlink。只有这个可选探测允许 cwd PermissionError：输出一次 `cwd unavailable; using cmdline/stat identity` 后继续，每次入口调用一次，不在进程扫描循环中重复输出。自身成功不能证明其他进程可读，因此每个候选仍独立处理错误。

`health.sh` 原本会把所有普通非零探测退出码折叠成 nonzero；现为退出 2 保留 environment 原因。JSON 字段仍叫 `status`，未新增重复的 probe_status 字段；其值为任务要求的 FAILED。整个 health 的服务失败退出码仍是 20，控制器自身配置/文件错误的退出码仍是 2。普通退出 1、10、20、124/127 的既有分类、超时与命令缺失识别保持。prerequisite 失败仍沿用 WAITING/waiting 契约。

health 按既有协议丢弃探测 stdout/stderr，因此从 health JSON 看 environment 分类；直接执行 adapter 可见具体权限路径和能力诊断。没有为了日志改变控制器的输出隔离协议。reconcile 遇到 FAILED 仍走原修复流程，approver-repair 自身重新校验，权限不足时退出 2，不启动进程或发送信号。

## 本地验证

原始输出见 [M5_VALIDATION.txt](M5_VALIDATION.txt)。均只在本地运行，未连接 muse01，未 commit 或 push。

| 验证 | 结果 |
|---|---|
| 原 M1 fixture | PASS |
| M2 regression | PASS |
| M2C | 7/7 PASS |
| M3 | 14/14 PASS |
| M4 | 23/23 PASS（cwd 用例按上节更新） |
| 新增 M5 | 8/8 PASS |
| M4 反证 | 23/23 被对应断言抓到 |
| M5 反证 | 8/8 被对应断言抓到 |
| health.sh Bash 语法、git diff --check | PASS |

M5 的关键正例是真实相对脚本子进程、真实 pidfile/stat，注入对所有 /proc cwd/exe 访问的 PermissionError：identity 和 check 成功，stale repair 到达受控的 TERM 调用，访问记录仅包含预检的自身 cwd。另对 proc 目录 stat、目标 cmdline、目标 stat 分别注入 PermissionError，验证 identity/processes 抛错，main(check/repair)=2 且无启动/信号。独立 Python 子进程运行真实 `__main__` 入口，注入目标 cmdline 拒绝，验证退出 2，再由 health 消费相同故障，验证 FAILED/environment。真实子进程退出后验证 None 和 check=1。

新增测试还覆盖相对 pidfile 错 PID、缺失/非法、启动前 mtime、未来 mtime，信号前 starttime 变化，自身必需字段预检失败，以及 pidfile、日志、全局 proc stat、pidfd 权限失败。

## 反证验证表

每项只修改 impl 临时副本；runner 要求出现断言 FAIL，不能把语法/运行 ERROR 当作抓到破坏。

| M5 破坏 | 对应测试 | 实际失败 |
|---|---|---|
| 拒绝所有相对 daemon | relative_identity_without_cwd | None 不等于真实 starttime |
| 跳过 pidfile PID 比较 | relative_pidfile_binding | IdentityUnavailable not raised |
| 跳过 pidfile 时间比较 | relative_starttime_evidence | IdentityUnavailable not raised |
| 接受其他相对脚本路径 | relative_script_exact | starttime is not None |
| identity 吞掉 PermissionError | permission_errors_are_environment | 三个字段均 PermissionError not raised |
| 环境异常返回 1 | entrypoint_environment_mapping | 1 != 2 |
| 跳过预检 | preflight_required_fields | cmdline/stat 均 1 != 2 |
| environment 降级为 nonzero | entrypoint_environment_mapping | JSON reason nonzero != environment |

M4 的 23 项反证完整输出保存在验证文件；包括 UID、node basename、恢复 cwd 依赖、绝对 script 路径、`--check`、僵尸、starttime 字段、信号前 token 比较、stopped 选择、stop 返回码、日志阈值/mtime/末条事件、探针权限、alias、进程/日志存在性、stop marker、同名异目录进程、TERM、precheck、external 修复隔离、避免多余安装。全部得到预期断言失败。

## 尚存的沙箱依赖与验收边界

| 依赖 | 风险与处理 |
|---|---|
| `/proc/<pid>` 目录所有者 UID、目录枚举 | hidepid/权限策略可能阻止枚举或读取；实际 OS 错误退出 2。若平台完全隐藏条目而不报错，自身预检无法发现。目录 UID 沿用旧契约，非 dumpable 进程的 proc 所有者也可能受内核规则影响。 |
| `/proc/<pid>/cmdline`、`stat` | 用户已确认目标环境可读；其他实例/上下文仍可能拒绝，届时退出 2。空 cmdline 不授权；Z 状态仍拒绝。 |
| `/proc/stat` 的 btime | 相对路径交叉校验新增依赖；本机可读，任务未提供 muse01 的该项实测。读取失败退出 2，不跳过时间校验；绝对路径实例不依赖它。 |
| `/proc/self/cwd` | 仅可选能力诊断，权限失败记录后继续；目标进程 cwd 完全不读取。 |
| `/proc/<pid>/exe`、`environ`、`fd` 等 | 生产身份校验完全不读取这些 ptrace 敏感项。 |
| `pidfd_open`、`pidfd_send_signal` | 沙箱可能拒绝系统调用；OS 错误返回 2，停止当轮修复，仍不通过数字 PID 的 kill 绕过。 |
| pidfile、业务日志、stop marker 及目标路径 | 文件系统权限也可能限制读写；传播的权限错误归为环境错误。日志内容无效与权限拒绝分开。 |

这里的沙箱验证是可重复的 PermissionError 注入，未在 muse01 平台运行。实际部署所有者仍需在真实 controller 上验证相对实例的 btime/pidfile 时间条件和信号权限；本次授权范围内的本地实现、回归与反证已完成。
