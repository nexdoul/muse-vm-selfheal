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
`nonzero`，Bash 找不到命令且最终退出 127 为 `command_missing`，超时为 `timeout`。
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

1. **volume 挂载来源，22–27 行**。设置 `VOLUME_PATH` 和 `SOURCE_PATTERN`，后者对应
   原检查的 `/dev/mapper/rv`。保留原来的 grep 子串匹配语义。

   ```bash
   findmnt -n -o SOURCE "$VOLUME_PATH" | grep -q "$SOURCE_PATTERN"
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

`config/services.muse02.json` 是从真实七服务清单提取的只读健康清单，保留 ID、目标状态、类型和重试参数。
修复入口和业务说明不进入此执行器；它不负责部署或 reconcile。检查使用绝对路径，工作目录设为 `/`。
部署前把 `environment` 内所有尖括号占位符替换为**取部署实际值**的配置；不要把私有值复制到文档。
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

keepalive 无账号时正常 exit 0、unit inactive 是设计终态，但不等于满足 running。
清单要求 running 时仍 FAILED；确实要求停用时设 `desired_state: stopped`，由 stopped_check 验证
unit 已加载且 inactive 后显示 STOPPED。不存在的 unit、查询失败、failed 状态均不算已停止。
运行检查同时要求 unit active 与日志 mtime 新鲜；mtime 只证明最近写入，不能证明刷新业务成功。

每轮原子发布 `state-dir/status.json`，包含组件 reason 和以下元数据：

- `generation`：读取 `run-dir/generation`，缺失为 `unknown`。
- `boot_id`：读取内核 boot_id；`config_version`：清单 version 的字符串值，沿用旧协议类型。
- `checked_at`：完成检查时的 UTC ISO8601 时间。
- `last_success`：本轮整体 READY 时更新，否则保留已有 status.json 中的值，初始为空字符串。
- `consecutive_failures`：整体 FAILED 时递增，READY 或 WAITING 时归零。

读上轮状态和写新状态都在现有锁内；已有状态损坏时退出 2，保留原文件。

验证新协议和检查语义：`python3 impl/tests/test_m2.py`。
原 M1 夹具保持不变；详细改写对照及任务指定命令的原始输出见 `M2A_REPORT.md`。
