# 清单驱动健康检查

运行无害示例：

```bash
impl/bin/health.sh --services impl/config/services.example.json \
  --state-dir /tmp/x/state --run-dir /tmp/x/run --json
```

每个服务产生一条 `components` 记录；目标全部满足时退出 0，存在 WAITING 且没有 FAILED 时退出 10，任一 FAILED 时退出 20。
检查输出全部丢弃，stdout 只输出结果 JSON。结果通过同目录临时文件加 `mv`
原子发布到 `status.json`。控制器参数、清单或文件操作错误退出 2。

`desired_state` 默认 running，执行 `health_check`；stopped 只执行 `stopped_check`。
缺少所需检查不是健康成功，而是清单错误。`health_timeout_sec` 默认 5 秒，必须大于 0，
每次探测覆盖环境加载、工作目录切换和检查执行（每条 prerequisite 单独计时）。超时先向进程组发 TERM，1 秒后发 KILL；
检查不得通过新建会话逃离进程组。普通非零退出（含显式 `exit 124`、`exit 127`）为
`nonzero`；探测命令退出 2 专用于环境错误，映射为 FAILED/environment，整体 health 仍退出 20。
Bash 找不到命令且最终退出 127 为 `command_missing`，超时为 `timeout`。
各项顺序执行，不会因前一项失败而跳过后续服务。锁位于 run-dir，竞争时等待。

## 服务环境

执行从 `env -i` 开始，仅预设固定系统 PATH、HOME、USER、LOGNAME；Bash 会维护 PWD、
SHLVL 等自身变量。`user` 默认当前用户，`home` 默认该用户 passwd 中的 home，
`workdir` 默认 home。指定不同用户需要 root 和 `runuser`；无法切换或用户不存在时
该服务 FAILED/nonzero，绝不冒充目标用户执行。

进入 workdir 后加载 `environment_file`，再应用字符串字典 `environment`（优先级更高）。
环境文件是**可信的 Bash 赋值脚本**，自动导出其中变量，不是 systemd EnvironmentFile
语法解释器；相对路径以 workdir 为基准，建议使用绝对路径。示例用 `/dev/null` 表示
空环境文件；实际可换成只允许服务用户读取的文件，例如内容为 `PROBE_VALUE=from-file`。
环境字段与检查字符串均属于可信配置，可执行代码；加载与检查的 stdout/stderr 均不回显。
调用者的 HOME、PROBE_LEAK、BASH_ENV 等不会传入服务。

依赖 Bash、jq、setsid、mkfifo、env、flock、mktemp、id、getent 和常规文件工具。
本执行器仅检查本地状态，不执行 start/stop/repair，也不解析依赖拓扑。

## 从原硬编码检查迁移的七个示例

以下引用均指 `_evidence/muse02/selfheal/bin/health.sh` 的行号。
代码块是可直接写入清单的 `health_check` 字符串内容（存入 JSON 时需要正常转义），
每项建议 `health_timeout_sec: 5`。原来的路径、unit、端口和匹配条件改由 `environment`
传入；参数须取部署上的原值，下面不包含真实主机地址。

1. **volume 挂载来源，22–27 行**。设置 `VOLUME_PATH` 和 `VOLUME_SOURCE`，后者取部署记录的来源模式。
   保留原来的 grep 匹配语义，并检查 findmnt 自身是否成功。

   ```bash
   source=$(findmnt -n -o SOURCE "$VOLUME_PATH") || exit 1
   printf "%s\n" "$source" | grep -q -- "$VOLUME_SOURCE"
   ```

2. **两个配置的版本和可解析性，29–35 行**。设置 `SITE_CONFIG`、`SERVICES_CONFIG`。

   ```bash
   jq -e '.version == 1' "$SITE_CONFIG" && jq -e '.version == 1' "$SERVICES_CONFIG"
   ```

3. **sshd unit 和监听端口，46–52 行**。设置 `UNIT` 和 `LISTEN_ADDRESS` 为原部署值。

   ```bash
   systemctl is-active --quiet "$UNIT" || exit 1
   listeners=$(ss -tlnH) || exit 1
   printf "%s\n" "$listeners" | awk -v address="$LISTEN_ADDRESS" '$4 == address { found=1 } END { exit !found }'
   ```

4. **隧道进程，54–67 行**。每个 relay 生成一项，`REVERSE_PORT`、`RELAY_HOST`
   取自 site 配置对应条目；与原逻辑一样，在端口配置完成后才执行这些检查。

   ```bash
   host=$(printf "%s" "$RELAY_HOST" | sed 's/[][\\.^$*+?(){}|]/\\&/g') || exit 1
   pgrep -f "(^|/)[s]sh[[:space:]].*[[:space:]]-R[[:space:]]+[^[:space:]]*:${REVERSE_PORT}:[^[:space:]]*[[:space:]].*(@|[[:space:]])${host}([[:space:]]|$)" >/dev/null
   ```

5. **审批工作进程及心跳新鲜度，69–95 行**。设置 `APPROVER_CONFIG` 和 `APPROVERCTL`。
   单次读取状态，验证 PID 存活及心跳在过去 120 秒内。进程身份仍依赖 approverctl 的 worker_pid 契约。

   ```bash
   jq -e '.version == 1' "$APPROVER_CONFIG" >/dev/null || exit 1
   desired=$(jq -r '.desired_state // "stopped"' "$APPROVER_CONFIG") || exit 1
   enabled=$(jq -r '.enabled // false' "$APPROVER_CONFIG") || exit 1
   if [ "$desired" = stopped ] || [ "$enabled" != true ]; then exit 0; fi
   st=$("$APPROVERCTL" status) || exit 1
   pid=$(printf '%s' "$st" | jq -er '.worker_pid | select(type == "number" and . > 0 and . == floor)') || exit 1
   last=$(printf '%s' "$st" | jq -er '.evidence.last_list_success_at | select(type == "string" and length > 0)') || exit 1
   kill -0 "$pid" 2>/dev/null || exit 1
   last_epoch=$(date -d "$last" +%s 2>/dev/null) || exit 1
   now_epoch=$(date -u +%s) || exit 1
   age=$((now_epoch - last_epoch))
   [ "$age" -ge 0 ] && [ "$age" -le 120 ]
   ```

6. **parse API，97–109 行**。设置 `UNIT`、`HEALTH_URL`（原服务的本地 `/healthz`）。
   这里等价于原代码中 desired running 分支。

   ```bash
   systemctl is-active --quiet "$UNIT" && curl -fsS -m 3 "$HEALTH_URL"
   ```

7. **clip MCP，111–122 行**。为另一服务配置其自身的 `UNIT`、`HEALTH_URL`，同样是
   desired running 分支。停止目标用单独的 `stopped_check` 证明，例如以下停止检查
   同时排除 systemctl 的查询错误，适用于已安装且已停止的 unit：

   ```bash
   state=$(systemctl show --property=ActiveState --value "$UNIT") || exit 1
   [ "$state" = inactive ]
   ```

   运行检查仍为：

   ```bash
   systemctl is-active --quiet "$UNIT" && curl -fsS -m 3 "$HEALTH_URL"
   ```

## 新清单与协议

`config/services.muse02.json` 包含原七服务和前置的 volume/config/tunnels，共十项。
三个前置项标记 `kind: infrastructure`、`ownership: external`、`repair: null`；reconcile 消费此清单时须跳过这些外部所有者条目的修复。
修复入口和业务说明不进入此执行器；它不负责部署或 reconcile。检查使用绝对路径，工作目录设为 `/`。
部署前替换 `environment` 和 `desired_state_ref.file` 内所有尖括号占位符，值取部署实际配置；不要把私有值复制到文档。
未替换引用路径时扫描退出 2，不能把此模板的本地运行当作服务健康结果。
隧道端口是远端监听端口，主机字段只填主机地址，不含用户名前缀。日志阈值默认 120 秒，应按实际日志周期调整。

```bash
impl/bin/health.sh --services impl/config/services.muse02.json \
  --state-dir /tmp/m2/state --run-dir /tmp/m2/run --json
```

`prerequisites` 是 shell 命令字符串数组，使用与健康检查相同的用户、隔离环境和超时回收机制。
running 时顺序执行，任一不成功（包括超时、命令缺失）即 WAITING/waiting，跳过该健康检查；
stopped 不执行运行前置条件。健康检查自己的 `exit 10` 仍是 FAILED/nonzero。
缺省无前置条件。隧道占位符未配置时 WAITING；配置齐全但无匹配进程时 FAILED。
FAILED 的整体优先级始终高于 WAITING，和服务顺序无关。

顶层 `status_protocol: 2` 开启 STOPPED/stopped：`desired_state: stopped` 的停止检查通过后返回该标签。
未声明该字段（或为 1）的旧清单继续返回 READY/ready，以保持 M1 原夹具的契约。
两种协议均支持 WAITING 和元数据，STOPPED 不影响整体退出 0。
**M1 确实断言 stopped 成功为 READY，因此“无条件改成 STOPPED”和“不改夹具且全通过”不能同时成立。**
新清单显式选择协议 2；调用者迁移时必须同步接受 STOPPED 标签。

审批服务设 `user: root`，因为部署的 approverctl 仅 root 可执行。
非 root 控制器无法切换用户时该项 FAILED；生产需由 root 执行控制器。
审批自身配置 stopped 或 disabled 时按原规则满足；清单仍为 running 时显示 READY。
worker_pid 必须指向存活进程，状态只查询一次，过去 120 秒内 list 成功才通过；未来或无效时间失败。

keepalive 通过下述数组规则读取 accounts：空数组要求 stopped，非空要求 running。
停止检查要求 unit 已加载、inactive、Result=success、ExecMainStatus=0；满足后显示 STOPPED。
不存在的 unit、查询失败、failed 状态和非零退出均不算设计终态。
运行检查同时要求 unit active 与日志 mtime 新鲜；mtime 只证明最近写入，不能证明刷新业务成功。

### 从业务配置读取目标状态

清单中用引用替换字面量（两字段同时存在，包括 null，退出 2）：

```json
"desired_state_ref": {"file": "<PRIVATE_CONFIG>", "pointer": "/desired_state"}
```

引用在控制器身份下、所有探测之前读取；相对路径以调用者当前目录为基准，部署建议使用绝对路径。
JSON Pointer 支持空指针（根值）、空键、`~0`/`~1` 转义、数组下标；不支持 URI fragment 形式。
文件必须恰好包含一个 JSON 文档。缺文件、非法 JSON、缺失路径、非法转义或值不为
running/stopped 均退出 2，保留上次 status.json。

keepalive 使用同一引用上的派生规则（A）：

```json
"desired_state_ref": {
  "file": "<KEEPALIVE_CONFIG>", "pointer": "/accounts",
  "when_empty": "stopped", "when_nonempty": "running"
}
```

两条映射必须同时提供且均为合法状态，引用值必须为数组；对象、字符串、null 不当成空账号。
这样目标只由业务配置导出，无第二个可写状态字段。选定目标后沿用原协议、前置检查和停止检查逻辑。

`tunnels` 的 prerequisite 检查 `relays[]` 中缺失或为 null 的 reverse_port 数量为零；
不满足即 WAITING/exit 10，全部已配置则执行 `exit 0` 得到 READY。
这不把普通 health_check 的 exit 10 改成 WAITING。volume/config 失败仍为 FAILED/exit 20，
且整体 FAILED 优先于 tunnels 的 WAITING。数组前置顺序用于报告，不增加隐式依赖或阻止其他条目被检查。

每轮原子发布 `state-dir/status.json`，包含组件 reason 和以下元数据：

- `generation`：读取 `run-dir/generation`，缺失为 `unknown`。
- `boot_id`：读取内核 boot_id；`config_version`：清单 version 的字符串值，沿用旧协议类型。
- `checked_at`：完成检查时的 UTC ISO8601 时间。
- `last_success`：本轮整体 READY 时更新，否则保留已有 status.json 中的值，初始为空字符串。
- `consecutive_failures`：整体 FAILED 时递增，READY 或 WAITING 时归零。

读上轮状态和写新状态都在现有锁内；已有状态损坏时退出 2，保留原文件。

验证新协议和检查语义：`python3 impl/tests/test_m2.py`、`python3 impl/tests/test_m2c.py`。
反证验证：`python3 impl/tests/test_m2c.py --mutations`，仅破坏临时副本，原始输出见 `M2C_VALIDATION.txt`。
原 M1 夹具保持不变；详细改写对照及任务指定命令的原始输出见 `M2A_REPORT.md`。

## 本地 reconcile

先查看条件动作计划，再执行同一清单：

```bash
impl/bin/reconcile.sh --services /tmp/release/config/services.json \
  --state-dir /tmp/controller/state --run-dir /tmp/controller/run --validate
impl/bin/reconcile.sh --services /tmp/release/config/services.json \
  --state-dir /tmp/controller/state --run-dir /tmp/controller/run --plan
impl/bin/reconcile.sh --services /tmp/release/config/services.json \
  --state-dir /tmp/controller/state --run-dir /tmp/controller/run --json
```

`--validate` 仅校验 schema，不读取目标引用文件、不运行任何服务命令。
`--plan` 解析目标引用，输出条件动作 JSON 数组；不运行健康探测、prepare、repair 或 systemctl，
也不写 state/run 目录。每项的 `when` 表示真正执行时仍须满足的条件；计划不声称当前服务已坏。
没有两个模式选项时才执行。参数/清单/引用错误退出 2，reconcile 锁竞争退出 10。

`repair: null` 和 `ownership: external` 都禁止 prepare 与修复，只观察健康状态。
`repair` 字符串或非空字符串数组按顺序执行；数组中任一步失败即停止该项修复。
缺省 repair 时用 `unit`（或 `environment.UNIT`）执行 systemctl start/stop。
running 且目标检查失败、prerequisites 通过时，先顺序执行 `prepare` 数组；stopped 跳过 prepare
和运行前置条件。默认 systemctl 修复在 Result=start-limit-hit 时先 reset-failed。

每次修复后等待 `settle_sec`（默认 3 秒，可设 0），再复查；命令超时使用 `timeout_sec`（默认 30 秒）。
命令与探测复用 health.sh 的用户、环境和超时回收机制。默认 unit 的终态判定接受 active，
或 inactive + Result=success + ExecMainStatus=0；停止目标只接受后者。随后仍须通过清单的目标检查，
所以有额外业务条件的服务不会仅因进程正常退出就被报绿。自定义 repair 的成功条件由目标检查定义。

末尾使用原样 `--services/--state-dir/--run-dir` 调用 health.sh 并输出其结果；退出码取本轮修复
和最终扫描中较严重者（FAILED=20 优先于 WAITING=10）。因此，修复命令非零但最终探测已绿时，
health JSON 可为 READY，reconcile 仍退出 20；自动化必须同时检查退出码。中间探测写入临时目录，
不会增加正式 status.json 的失败计数。`kind` 只作清单元数据，核心不按 kind 或服务 ID 分支。

新增 `config/services.muse02.reconcile.json` 是完整的十项迁移模板：审批用 desired_state_ref，
本地 sshd 用 prepare，隧道用 repair:null + ownership:external + prerequisites。
原 `services.muse02.json` 保持 M2/M2C 的健康检查入口契约，不直接用于新 reconcile；
它的审批项仍按上文旧检查规则工作。新模板的审批停止检查验证 worker_pid 为空或零；
running 前置条件检查 enabled 和停止标记，修复使用 unit，不调用会清除停止标记的 approverctl start。
新模板必须把 desired_state_ref.file 与 APPROVER_CONFIG 配为同一个审批配置文件。
原配置缺 desired_state 时旧脚本默认 stopped，新引用会明确退出 2，部署时需填写合法目标值。

切换接口与 release 布局见 [migration/README.md](migration/README.md)。
M3 测试：`python3 impl/tests/test_m3.py`；反证：`python3 impl/tests/test_m3.py --mutations`。

## 重置后的 unit 恢复（M6）

将完整 release（包括 `units/`）放在持久卷，渲染 `config/services.json`，为清单中每个非 external unit 提供模板，然后运行：

```bash
impl/bin/bootstrap.sh --services impl/config/services.json --dry-run
impl/bin/bootstrap.sh --services impl/config/services.json
```

bootstrap 只安装/启用，不启动业务服务。相同内容不重写、不无谓 reload；模板缺失明确失败。
controller 启动前自举；独立平台入口调用 `bin/boot-ensure.sh`，负责 controller unit 丢失后的首次启动。
keepalive repair 调持久卷 installer，再按 accounts 执行 start/stop。两个 keepalive unit 的检查在
LoadState=not-found 时通过内部 FD 3 标记 `missing_unit:` 并退出 1，health 发布 FAILED/missing_unit；
loaded 但不健康仍为 nonzero。其他旧检查不会新增 systemctl 查询。

完整发布布局、cf-probe 模板要求、`started` 失败不重试的 init 改造方案见 [M6_REPORT.md](M6_REPORT.md)。
