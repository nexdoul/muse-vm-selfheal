# 清单驱动健康检查

运行无害示例：

```bash
impl/bin/health.sh --services impl/config/services.example.json \
  --state-dir /tmp/x/state --run-dir /tmp/x/run --json
```

每个服务产生一条 `components` 记录；全为 READY 时退出 0，任一 FAILED 时退出 20。
检查输出全部丢弃，stdout 只输出结果 JSON。结果通过同目录临时文件加 `mv`
原子发布到 `status.json`。控制器参数、清单或文件操作错误退出 2。

`desired_state` 默认 running，执行 `health_check`；stopped 只执行 `stopped_check`。
缺少所需检查不是健康成功，而是清单错误。`health_timeout_sec` 默认 5 秒，必须大于 0，
覆盖环境加载、工作目录切换和检查执行。超时先向进程组发 TERM，1 秒后发 KILL；
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
   systemctl is-active --quiet "$UNIT" && ss -tln | grep -q "$LISTEN_ADDRESS"
   ```

4. **隧道进程，54–67 行**。每个 relay 生成一项，`REVERSE_PORT`、`RELAY_HOST`
   取自 site 配置对应条目；与原逻辑一样，在端口配置完成后才执行这些检查。

   ```bash
   pgrep -f "ssh.*-R.*${REVERSE_PORT}.*${RELAY_HOST}"
   ```

5. **审批工作进程及心跳新鲜度，69–95 行**。设置 `APPROVER_CONFIG` 和 `APPROVERCTL`。
   保留原代码对 worker_pid 非空和心跳不超过 120 秒的判定，包括原代码对未来时间的处理。
   原代码依赖 approverctl 提供进程身份验证，本表达式同样依赖它。

   ```bash
   jq -e '.version == 1' "$APPROVER_CONFIG" || exit 1
   desired=$(jq -r '.desired_state // "stopped"' "$APPROVER_CONFIG")
   enabled=$(jq -r '.enabled // false' "$APPROVER_CONFIG")
   if [ "$desired" = stopped ] || [ "$enabled" != true ]; then exit 0; fi
   pid=$("$APPROVERCTL" status | jq -r '.worker_pid // empty')
   last=$("$APPROVERCTL" status | jq -r '.evidence.last_list_success_at // empty')
   [ -n "$pid" ] && [ -n "$last" ] || exit 1
   last_epoch=$(date -d "$last" +%s 2>/dev/null || echo 0)
   now_epoch=$(date -u +%s)
   [ $((now_epoch - last_epoch)) -le 120 ]
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

这些表达式覆盖原检查的命令与条件，但本次尚未完整迁移旧控制器的输出协议：
原 37–44 行的 WAITING/退出 10、审批 STOPPED 标签，以及 generation、boot_id、
连续失败数、last_success 等元数据未实现。新协议按 M1 仅输出 READY/FAILED；
停止目标满足时也是 READY。旧服务清单中使用 `and` 和说明性文字的 health_check
必须先改成合法 shell 命令；不能直接把旧清单交给新执行器来替换生产检查。
