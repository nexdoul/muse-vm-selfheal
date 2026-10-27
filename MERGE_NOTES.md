# muse01 / muse02 自愈方案合并记录

交付是通用 skill 与迁移契约，未部署到 VM、未修改证据脚本。以 muse02 的声明式框架为骨架，吸收 muse01 的离线恢复、环境纠正与身份校验；每个组件保留唯一所有者。现有 1–10 章及自动审批的策略、维护和授权边界保留。

## 1. 证据与采纳的设计

下列路径均相对 `_evidence/`，行号对应此次证据快照。已通读全部脚本（含备份版本）、config、unit、hook definitions；BRIEF 的前置实测结论直接采信，未重做运行调查或重复校验文件摘要。

| 来源与文件行号 | 具体设计与合并处理 |
| --- | --- |
| `muse01/workspace/setup/watchdog.sh:9–13,26–39,110–114` | HOME 异常纠正；单实例锁与发信号前 cmdline 校验。泛化为 site 声明 HOME，强化 UID/starttime/运行代次，防 PID 复用 |
| `muse01/workspace/setup/restore-all.sh:28–50` | 恢复锁与期限。吸收互斥目的，不继承“活进程只是超龄就删锁”的并发风险；原 owner 退出后才能移交 |
| `muse01/workspace/setup/lib-pkgs.sh:8–18,31–46,65–74` | 包管理锁有界等待、只补缺包、离线重试。保留这些原则，不把两次 dpkg 当依赖闭包验证 |
| `muse01/workspace/setup/install-cf-probe.sh:8–18,38–52,63–96` | 安装器＋缓存二进制双兜底；改成固定版本、本地优先恢复。原来先下载浮动安装器，不作为快速路径 |
| `muse01/workspace/setup/install-cf-probe.sh:5–6,26–30,47–49` | 声称秘密只落一处，但实际内嵌并通过 argv 传入。采纳单一来源意图，改为秘密文件引用，不能原样复制安装逻辑 |
| `muse01/workspace/setup/cache-cf-probe-bin.sh:12–40` | 按架构缓存实际二进制、运行自检、临时文件验证后发布。补入缓存运行时/动态库和离线安装验收 |
| `muse01/workspace/setup/run-probe-install.sh:2–11,19–33` | 记录安装器按关键字误杀调用者与临时位置破坏缓存定位的问题。保留隔离/绝对定位要求，禁止复制含秘密脚本作为通用解决方案 |
| `muse01/workspace/setup/restore-all.sh:79–87,131–140` | 尊重独立 SSH 与用户钉版本自愈；保留网关同步/后端恢复职责，不因框架化而丢弃 |
| `BRIEF.md` 第二节 2.3 | 探针 `Restart=on-failure`，远端续期证书、本地持久回落、网关与后端链路。对应 unit 和 nginx/ensure.sh 本体未随证据提供，此处来源是已确认调查，不虚构脚本行号 |
| `muse01/workspace/setup/backup-hermes.sh:7–23,48–66` | 活跃 SQLite 使用在线备份、失败不回退裸复制。保留现有 skill 第 8 节，不重新启用已退役备份入口 |
| `muse02/selfheal/config/services.json:32–123` | 以 services 数组声明服务；改为真正驱动 health/reconcile 的唯一清单，而非说明性配置 |
| `muse02/selfheal/bin/reconcile.sh:12–16,23–72` | flock 与清单循环。把审批来源、sshd prepare、隧道上下文三处特例移入字段/适配器，补依赖拓扑和单一目标状态来源 |
| `muse02/selfheal/bin/health.sh:1–4,124–154` | 0/10/20 分层状态与原子 JSON 输出。区分单次探测结果与恢复状态，补稳定优先级、每服务状态、控制器错误和新鲜度 |
| `muse02/selfheal/bin/supervisor.sh:2–10`；`units/muse-selfheal-supervisor.service:8–15` | 单一前台调度循环及监督器管理。保留结构，扫描预算随清单计算，不保留固定 120 秒掩盖长扫描的行为 |
| `muse02/selfheal/bin/bootstrap.sh:14–45` | 卷校验、运行代次、从持久 unit 重建注册。卷身份从 site 读取，bootstrap 不再另列业务启动分支 |
| `muse02/selfheal/bin/platform-hook.sh:11–16,29–57` | 引导＋状态驱动通知。保留分层，修正固定总超时和无限 WAITING 风险，通知按故障/代次去重 |
| `muse02/selfheal/bin/tunnel-ensure.sh:2–5,13–43`；`bootstrap.sh:62–69` | 特定环境必须 agent 上下文才能建立隧道；泛化为 execution_context，不假设 systemd 能取得相同出网能力，不复制宽泛 pkill |
| `muse02/selfheal/bin/sshd-prepare.sh:18–41`；`sshd-run.sh:7–30` | 临时用户/目录重建、持久主机密钥与前台 sshd。保持可用入口，不照搬全局禁用发行版 ssh 的部署特例 |
| `muse02/selfheal/config/approver.json:3–17`；`bin/approverctl:24–31,61–75,161–174` | 私有策略/凭据引用、停止标记及进程身份。实际 approverctl 已校验 cmdline/starttime，因此不是 muse02 完全没有 PID 校验；缺的是全框架统一契约 |
| `muse02/selfheal/units/muse-selfheal-gemini-keepalive.service:28–35` | 正常等待/配置终态禁止无限 Restart；reconciler 也必须服从维护/预算，不能绕过 unit 的重启限制 |

## 2. 改掉的内容与原因

| 原位置 | 修改及依据 |
| --- | --- |
| 第 12 行的探针排除 | 纳入外部 agent 通用契约，恢复真实维护职责；不捆绑服务端或拷贝秘密 |
| 第 2–4 章的路径/锁/责任 | 私有家目录换占位符，HOME 从固定 site 配置纠正；单一调度所有者、external 边界、完整 PID 身份与安全锁回收 |
| 第 5 章“建议”退出码 | 改成必须遵守 0/10/20，控制器异常固定 30；聚合只升严重程度，失败探测不自动等同于预算内恢复中 |
| 第 6 章仅列 health_check 字段 | 增加 6.1/6.2：shell、默认 10 秒及理由、工作目录/用户/环境、退出码、超时清理、输出和停止目标语义；禁止按服务 ID 硬编码 |
| 第 6 章缺服务类型 | 新增外部 agent、跨机证书两类契约；原长任务内容保留为 6.5 |
| 第 9 章先启新后停旧 | 改为影子验收→停止旧写入口并排空→启新入口；否则有双重修复窗口 |
| 第 9 章迁移泛化步骤 | 明确 M0–M6 两条来源路径、前置门槛、独立回滚；具体命令放入 references，skill 在相应位置链接 |
| 元数据与措辞 | 版本升为 1.2.0，description 纳入新服务类型；删除“建议”式执行规则，保留原中文表格与 1–10 章 |

关键纠错证据：

- `muse02/selfheal/bin/health.sh:46–122` 写死本地 SSH、审批和两个业务服务，没有遍历清单执行检查。`config/services.json:110–120` 已登记的新服务因此漏检（BRIEF 已确认）。
- 清单 `services.json:36,48,60,72,116` 包含 `and` 或自然语言；其中隧道 unit 又在 `bootstrap.sh:62–69` 明确不启用。不能只增加 shell 执行而不迁移这些字段。
- `reconcile.sh:38,56,77` 的状态赋值会让后来的等待覆盖前面的失败，且只特判 health 10/20；新契约禁止吞控制器错误。
- `health.sh:76–77` 仅因目标停止就报 STOPPED，没有验证实际退出；新契约须有 stopped_check/后端进程证据。
- `muse01/hooks/scripts/patrol-tunnel-watchdog.sh:2–3,22–25` 与 `patrol-ssh-reconcile.sh:2–3,20–21` 均通向同一隧道修复；对应 `definitions/patrol-*.json:5–8` 含实际入口/周期。`restore-all.sh:119–128` 又重装 watchdog cron。迁移必须同时处理注册及重新注册来源。
- patrol 脚本 dry-run 只限制状态写入，实际修复仍执行（如 `patrol-hermes-selfheal.sh:10–12,20–21`）；不能作为迁移只读影子检查。
- BRIEF 的“640 行死代码”作为历史清理候选采信，但执行顺序必须保留当前引用：`restore-all.sh:87`、`watchdog.sh:55` 调用 run-probe-install；`install-cf-probe.sh:70–72` 使用 saved 安装器。替代链路通过后退役，不能先删断离线恢复。

## 3. 风险与未决条件

1. **证书同步适配器待部署方补齐。** nginx/ensure.sh 未在证据集中；采信 BRIEF 的行为，但不声称审查了其原子性、有效期验证或 reload。迁移 M4 必须按新契约实现并验证后才能接管。
2. **探针秘密输入及上报接口待确认。** 当前包装器使用秘密 argv，证据没有探针二进制源码和服务端查询接口；不能假设已有文件/FD 支持。部署者须修补输入链路或选择兼容固定版本，取得真实接受记录；缺失时该组件保持旧所有者，不能标记迁移完成。证据中已有敏感值，交付未复制这些值；证据目录不得加入公开提交。是否轮换由凭据所有者按实际暴露范围处理。
3. **框架已有结构不等于契约已实现。** retry_budget、dependencies 等必须实际参与调度；hook 固定 15 秒扫描上限（platform-hook.sh:15）不能覆盖增加后的全量检查。文档中的新 CLI 要在目标实现生成，当前仓库未提供这些执行器。
4. **审批实现仍有门禁风险。** `approverctl:46–58,134–158` 未完整核验 strategy_confirmed，process substitution 中失败也不能自动保证外层 mapfile 失败；`health.sh:69–95` 未落实注释承诺的连续决策失败检查。沿用第 7 章的严格契约，真实决策前修复并验收，不从现有 config 的 true 值推断已满足全部门禁。
5. **旧备份不能直接担当回滚工具。** `muse02/selfheal/bin/backup.sh:31–35` 向压缩 tar 追加且吞错误，可能漏审批数据；`restore.sh:16–29,31–40` 的校验失败没有可靠终止后续解包。M0 独立制作并核验恢复材料，不能仅看旧脚本退出成功。`muse01/.../backup-hermes.sh:99–103` 逐库 rename 也不是多库事务，跨库一致性仍由业务机制保证。
6. **平台能力与自然重置仍需现场验收。** muse02 提供了 hook definition，但所指 wrapper 脚本未附带；不能推断 echo silent/wake 已按 runtime 注册。迁移者须读实际平台 API、验证注册回执、独立回滚通道及自然重置行为。

## 4. 分步迁移与验收

完整顺序、可复制命令和失败处置见 [references/migration.md](references/migration.md)。本表给出每步必须执行的入口；变量只在目标机私有记录中填写。

| 步骤 | 路径与依赖 | 验收命令/结果 | 回滚 |
| --- | --- | --- | --- |
| M0 | 两侧分别冻结；新入口停用 | 导出 `crontab -l`、实际 hook 注册、`systemctl show`；受限复制旧版本；独立通道运行 `true` | 无生产写入，无需切换 |
| M1 | muse02 → 通用；先于所有接管 | `bash -n`；`reconcile.sh --validate`；运行 reference 的 Python health 夹具，任意新 ID、0/非零/超时/不存在命令/环境/停止状态全部通过 | 保留旧框架运行 |
| M2 | muse02；依赖 M1 | `reconcile.sh --plan`；独立目录影子 `health.sh`；jq 比较清单与状态 ID；`systemd-analyze verify` | 不切换，修新版本 |
| M3 | muse02；依赖 M2 | `quiesce-old.sh` → `enable-new.sh` → `audit-owners.sh`；生产 health 两轮 READY、时间推进、端到端访问通过 | `quiesce-new.sh` → `restore-old.sh` → audit |
| M4 | muse01 → 通用；依赖 M3 | 本地二进制/权限检查；`check-agent-report.sh`；openssl 到期及线上 SNI 证书比对、curl HTTPS；隔离离线/过期故障矩阵 | 失败职责保留旧所有者，独立职责按映射继续 |
| M5 | muse01；依赖对应 M4 验收 | 同 M3 互斥切换；audit 与平台注册 readback；`rg -l` 核验活跃路径不引用退役脚本；新入口两个周期稳定后归档候选 | 关闭新写入口，再恢复预选单一旧调度，禁止整套重叠巡检复活 |
| M6 | 两侧自然重置后 | `bootstrap.sh --ensure`、health JSON、audit、SSH/TLS/上报/任务推进证据 | 独立通道按同一回滚顺序恢复入口 |

本次本地检查已通过：skill-creator 的 quick_validate、git diff --check、Markdown 本地链接检查、全部 Bash 示例语法检查、内嵌 Python 夹具语法检查。未运行目标 VM 的执行器或故障演练；上述 VM 验收属于后续部署执行，不把其列为已通过。

## 5. 第二轮修正

依据 `_evidence/ROUND2.md` 的真机调查记录，仅修正 A、B，版本从 1.2.0 升到 1.2.1；未执行部署或迁移。

- **A：6.3 凭据契约。** `cf-probe --help` 显示首次安装只有 `-secret=SECRET` 入口，并明确再次 install 沿用已有配置、仅覆盖显式参数；当前上游没有文件、FD、stdin 或环境变量秘密入口。首次安装 argv 是不可避免的唯一例外，安装前确认未安装，已有安装直接跳过，缩短暴露窗口。安装后立即核验 `/etc/config/cf-probe/config.conf` 为 0600、父目录为 0700，均为 root:root；稳态 run 从该文件读取，unit 的 `ExecStart=/usr/local/bin/cf-probe run -debug=0` 不含秘密。安装器清除明文及副本，快照不留含秘密脚本；以稳态单点存储和暴露窗口最小化验收。此结论取代上文第 3 节风险 2 中“修补输入链路或选择兼容固定版本”的要求，上报查询接口仍需部署方验证。
- **A：安装链路。** latest/saved 都是安装器脚本，简报确认两者 MD5 相同（前缀 `2ba70603…`），二进制另有缓存；沿用简报结论，不重复计算摘要。`restore-all.sh:87`、`watchdog.sh:55` 仍调用 `run-probe-install.sh`，替代链路验收后才能退役。
- **B：迁移授权。** 在第 9 章 M0 表格前及 `references/migration.md` 命令入口同步护栏：只读调查可进行，任何迁移写操作须逐阶段明确授权；仅更新配置或了解现状不得切换，未授权交付现状与待办。明确当前部署没有 M1 的 `--services/--state-dir/--run-dir/--json` 接口，先实现再验收。
- **范围外待同步措辞。** 第 3 章通用“密钥不放 argv”应结合 6.3 的首次安装唯一例外理解；迁移参考 M4 的“缺秘密输入接口不放行”应按修正后的 6.3 稳态存储与安装窗口契约验收，不能继续要求文件输入。按本轮约束仅记录，未改这些段落。
