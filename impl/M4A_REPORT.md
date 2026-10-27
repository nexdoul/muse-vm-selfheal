# M4-A：muse01 清单与适配器

已交付 `config/services.muse01.json`、`adapters/`、`deploy/cf-probe-secret-cleanup.md` 和 `tests/test_m4.py`。全部运行验证只使用本地临时目录、假命令及自建假守护进程；没有连接目标机，没有切换，没有 commit/push。清单保持 `status_protocol: 2`，所有私有路径、账户相关 home、主机地址、unit 与别名值都用占位符。固定协议字段、程序文件名、公开路径 `/` 和阈值不属于私有配置。

## 十项责任清单

| id | kind | ownership | repair 与依据 |
|---|---|---|---|
| volume | infrastructure | internal | null；观察持久卷挂载来源，不重建卷 |
| config | infrastructure | internal | null；两个 JSON 可解析且 version=1 |
| cf-probe | external-agent | managed | `cf-probe-repair.sh`；已加载但停止则 start，unit 缺失才调用经清理的 installer |
| approver | muse-auto-approve | managed | `approver-repair.sh`；保留 stop 标记、卡死 TERM、启动前预检与既有审批参数 |
| ssh-alias | ssh-alias | managed | `ssh-alias-repair.sh`；从 environment.ALIASES 检查全部别名，调用原本地 helper 恢复并复查 |
| gemini-keepalive | local-http | managed | null；业务 config 的 `/accounts` 是目标权威，空数组为 stopped |
| tunnel-krvps | ssh-reverse-tunnel | external | null；原 agent/ssh-infra 所有者负责，本框架只观察进程 |
| tunnel-usvps | ssh-reverse-tunnel | external | null；同上 |
| sshd | external | external | null；既有决策交由 ssh-infra，只观察指定监听地址 |
| hermes | external | external | null；保留用户的自愈入口，只观察指定进程模式 |

实际修复候选恰好三项。`--plan` 是条件动作计划，不探测实时故障；必须先注入私有值和可读取的 keepalive config。未经渲染的模板可 `--validate`，不保证直接 `--plan`，因为后者会解析引用。

## 三个适配器的行为

**cf-probe**：health 保持 active、二进制可执行、secret 文件权限恰为 600 三者同时满足。repair 不重装已加载 unit；start-limit-hit 先解除失败限制，普通停止直接 start。LoadState 查询错误、masked 等其他状态非零退出。缺 unit 时调用 installer，不传 secret 参数。installer 必须先按清理说明去掉自身的 secret argv 路径；本地假安装器通过不代表未改造的生产安装器已满足安全契约。

**ssh-alias**：拆为 check/repair。check 遍历清单 ALIASES，每个都必须有匹配的 Host 行；拒绝空集合、非法别名字符和仅有同名前缀。repair 缺项时才调用 ALIAS_REPAIR_HELPER，再按同一 ALIASES 复查。既有 helper 在私有环境中恢复持久化模板/密钥，适配器不覆盖无关配置。helper 不需要接受新增 CLI 参数；配置变量会通过 environment 导出，但其是否支持覆盖值需部署核对。若 helper 的模板缺少某个要求的别名，复查保持 FAILED，不伪造地址或误报成功。

**approver**：两个 shell 入口共用 `approver.py`，让 check/repair 使用相同的进程身份规则。检查 `/proc` 中同 UID、同工作目录、完整脚本路径、Node argv 的实例，排除 `--check` 和 zombie。PID 文件不是唯一依据：缺失或被旧 PID 覆盖时也不会重复启动。日志使用任务要求的 `daemon-log.ndjson` mtime，默认 900 秒，缺失、未来时间或过旧均失败。

卡死时取得 pidfd，并在发送 TERM 前重查 starttime 和身份；不向 PID 文件中无关进程发信号。发 TERM 的这一轮不启动替代进程，让后续一轮确认旧实例消失后再预检、启动。预检为 `node muse-daemon.cjs --check`，启动保留 `--loop 10000 --always --fallback-once`，通过 nohup 输出到 `log/muse-console.log`，不触碰健康证据日志的时间戳。没有 `sleep 3` 或启动后 pgrep 自证成功，结果交给 reconcile 的 settle/recheck。

stop 标记存在时显示 STOPPED、退出 0，repair 不清标记、不启动、不 TERM。依本任务，这是“用户要求不自愈”的终态；即使进程尚在也显示 STOPPED，不能据此声称进程已退出。marker 在解析后变化时检查可能短暂 FAILED，下一轮重新解析；没有引入第二份可漂移的 desired-state JSON。

## 为接入而增加的兼容接口

当前 M3 校验器只允许 internal/external，新增接受 managed；已有 ownership 值、默认 internal 和 external 禁修语义保留。

原 `stopped_check` 仅在 desired state 已是 stopped 时执行，不能自行切换状态。新增文件存在性引用，health 与 reconcile 都支持：

```json
"desired_state_ref": {
  "file": "<APPROVER_ROOT>/data/muse-daemon.stop",
  "when_exists": "stopped",
  "when_missing": "running"
}
```

此引用不允许同时包含 pointer 或数组映射；与字面 desired_state 冲突仍退出 2。存在性通过 stat 判断，找不到文件为 missing，其他访问错误退出 2。新引用使用 Python 3；审批的 pidfd 需要 Linux pidfd 支持和 Python 3.9+。部署主 Agent 必须确认其运行时支持；新引用不要求旧 M1/M2 调用安装新依赖或改变调用方式。

代码改动是 `lib/reconcile-manifest.py` 的兼容扩展，以及 `bin/health.sh` 在原 JSON Pointer 分支前的可选 marker 分支。`bin/reconcile.sh` 未修改；旧 JSON Pointer/数组规则与协议 1 的 stopped→READY 行为保留。M1/M2/M2C/M3 原测试文件均未改。

## 测试及反证

可复现命令：

```bash
python3 ../fixture_m1.py impl/bin/health.sh
python3 impl/tests/test_m2.py
python3 impl/tests/test_m2c.py
python3 impl/tests/test_m3.py
python3 impl/tests/test_m4.py
python3 impl/tests/test_m4.py --mutations
```

M1、M2 全部通过，M2C 7 项、M3 14 项通过；M4 15 项全部通过（退出 0），十个反证全部抓错。逐个 Bash 语法检查及 Python 编译检查通过。M4 覆盖完整十条扫描、validate/plan、三项 health 的正反路径、stop 文件直接切换、未知 PID/同名不同路径隔离、无 PID 文件避免双启动、TERM 后下一轮预检启动、预检失败、repair 环境传递与复查、keepalive 数组目标及四项 external 失败不修复。

反证在完整 release 的临时副本中逐一改坏实现，再运行对应行为测试。必须出现断言 FAIL 且退出非零；语法错误、导入失败或测试 ERROR 不计抓错成功。以下十个变异均已被断言抓到：

| 故意破坏 | 抓错证据 |
|---|---|
| cf-probe 忽略 secret 权限 | 644 错报 READY，预期 FAILED |
| alias check 恒成功 | 缺一个别名仍 READY |
| approver 忽略新鲜度 | 老日志仍 READY |
| approver 忽略进程存在 | 进程退出后仅凭新日志仍 READY |
| 忽略 stop marker | 有标记时报 FAILED，而非 STOPPED |
| 跳过完整脚本路径校验 | 同名但其他目录的脚本被当成目标 |
| 删除 TERM | 已确认的卡死假进程未退出 |
| 绕过预检失败 | 不允许启动时仍创建进程并报 READY |
| 删除 external 执行保护 | 注入的修复命令写出禁止标记；repair:null 没有掩盖 ownership 缺陷 |
| 停止的 probe 改走重装 | 未调用 start，错误调用 installer |

首次测试发现测试自身把之前的预检调用误算为新一次调用；已改为比较调用记录的前后差值，再通过完整测试。没有更改被测语义来迎合测试。

## 与 muse02 的关键差异

- muse01 是 2 个 infrastructure + 8 个服务/外部条目；muse02 是 3 + 7。这里没有独立的 tunnels 基础设施聚合项。
- 新增 cf-probe、ssh-alias；不含 douyin-parse/douyin-clip-mcp。sshd 明确 external，不使用 muse02 的托管 local-sshd。
- approver 直接适配旧 MuseAutoApprove daemon 与 stop 文件，未换成 muse02 approverctl/unit 契约。
- 可修复项为 3，muse02 reconcile 模板为 5。external 为 4（两个隧道、sshd、hermes），不是 5；ssh-infra 是所有者/入口，非额外清单 ID。
- keepalive 明确 repair:null；config/volume 设 internal + repair:null，不沿用 muse02 的 external 元数据。

## 部署交接与最大风险

最大风险是**停掉每分钟 watchdog 后，新 controller 或其调度入口不可用，cf-probe、审批、SSH 别名同时失去这条自愈路径**。尤其重置会丢失系统 unit、客户端 SSH 配置或二进制；仅有进程崩溃后的 Restart 不能处理 controller unit 本身丢失。SSH 别名的其他既有 helper 调用能否兜底没有本次验证证据，不能据此放心停 watchdog。

主 Agent 在 M4-B 前需完成三件具体工作：

1. 把所有占位值注入私有 manifest；APPROVER_ROOT 的环境值与 marker 引用必须一致，SERVICE_HOME/执行 UID、NODE_BIN 对准现有部署；ALIASES 注入任务给定的全部五项，ALIAS_REPAIR_HELPER 指向原持久化 helper。确认 keeper 的实际 unit 与日志周期匹配本模板（任务未提供其 unit 名）。把适配器与扩展框架作为同一 release 部署，不能只复制新清单给旧框架。
2. 在保留 watchdog 的阶段只做影子 health、validate、plan，不并行执行新 repair。清理 installer 后用隔离材料验证缺 unit 恢复；对审批预检、stop 终态、日志周期和别名恢复结果进行部署验收。这里的本地三条件 probe READY 仅证明本地状态，不证明服务端上报；隧道进程与 sshd 监听也不是端到端可达证明。
3. 为 controller 配置已存在体系中的 systemd 重启机制，并保留/改造平台 bootstrap hook：确认 controller 注册与存活、status 新鲜度，缺失时恢复 controller，持续失败唤醒主 Agent。该 hook 只监督 controller，不另建三个业务修复循环。移交时先停旧调度并等旧在途任务退出，再启新调度；失败则关新入口、等在途修复退出、恢复已备份的 watchdog cron。保留独立恢复通道和 crontab 备份。

还须明确：M3 通用切换模板管理 unit/hook，并不自动编辑共享 crontab；不能直接把 muse02 的切换命令当作 muse01 watchdog cron 已停的证据。两套隧道 hook 的已有职责重复、nginx 每周任务、home-init、hermes 与 ssh-infra 的既有入口均不在本次变更范围。本次没有部署 controller/兜底，也没有宣称目标机已验收。

## 任务描述需要纠正之处

1. 目标开头写“两个新适配器”，正文与验收实际要求三个；本次交付三个。
2. “repair 不注入 environment”不适用于当前 `2212cd9` 代码：`command_probe` 保留服务 environment，复用 health 的隔离执行器。M4 实测已验证；因此 repair 可直接引用环境变量，不必重复硬编码路径或 source 第二份配置。
3. “已通用化”仍有两个接口缺口：managed 尚未被校验器接受，stop 文件不能单靠 stopped_check 动态选目标。本次是向后兼容扩展，未改旧测试。
4. 原 watchdog 的卡死检测读取日志末尾事件的 `ts`，且只有读到 ts 才判卡死；任务要求使用 mtime，并把缺日志视为 FAILED。这是有意按新验收调整，不是严格等价于原逻辑；持续写错误日志可能掩盖业务卡死。
5. 迁移方案对照表写 external=5，但所列十项只有四项 external；ssh-infra 不是第十一项。
6. “config 存在则跳过 install”若理解为跳过整个恢复，会让 unit 丢失后永久无法恢复；应跳过凭据生成/重装健康组件，仍恢复缺失的注册。secret 清理不能拖到新 repair 已启用后的 M4-C。
