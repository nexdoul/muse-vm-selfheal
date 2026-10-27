# M2-A 实施与验收报告

## 结果与契约冲突

七服务新清单：`config/services.muse02.json`。执行器：`bin/health.sh`。M1 原夹具通过；逐条 bash -n 通过；本机扫描 5 FAILED、2 WAITING，退出 20。新增回归：`tests/test_m2.py`。

任务中“夹具只测 READY/FAILED 路径，新增 STOPPED 不会破坏它”的描述不准确：原夹具实际执行 `desired_state=stopped`，并断言成功组件是 READY。这与无条件 STOPPED 冲突。实现采用显式兼容版本：原清单缺省 `status_protocol=1` 保留 READY；新清单 `status_protocol=2` 返回 STOPPED。夹具完全未改。这是新增协议选择，不能声称未声明协议的旧清单也会输出 STOPPED。

以下原文对照中地址与部署 home 已脱敏为占位符，以遵守不复制私有值的约束；其余命令文本来自证据原文件。完整原文仍在 `_evidence/muse02/selfheal/config/services.json`，未修改。私有部署值须在 environment 中取部署实际值。

## 七条改写对照

### local-sshd

原文（地址和部署 home 脱敏）：

```text
systemctl is-active muse-selfheal-sshd.service and ss -tln <ADDRESS>:22
```

新文：

```bash
systemctl is-active --quiet "$UNIT" || exit 1
listeners=$(ss -tlnH) || exit 1
printf "%s\n" "$listeners" | awk -v address="$LISTEN_ADDRESS" '$4 == address { found=1 } END { exit !found }'
```

任务证据：独立 ss 命令输出 `syntax error!`，退出 255；原组合命令退出 1。改为 unit quiet 检查和 `ss -tlnH`，独立检查 ss 退出码，再精确匹配本地地址列。任务建议的 grep 子串会误中相邻端口；awk 精确比较消除误报。地址来自 environment。

### tunnel-krvps

原文（地址和部署 home 脱敏）：

```text
systemctl is-active muse-selfheal-tunnel-krvps.service and pgrep -f ssh.*-R.*<ADDRESS>
```

新文：

```bash
host=$(printf "%s" "$RELAY_HOST" | sed 's/[][\\.^$*+?(){}|]/\\&/g') || exit 1
pgrep -f "(^|/)[s]sh[[:space:]].*[[:space:]]-R[[:space:]]+[^[:space:]]*:${REVERSE_PORT}:[^[:space:]]*[[:space:]].*(@|[[:space:]])${host}([[:space:]]|$)" >/dev/null
```

任务证据：unit 输出 `inactive` / `disabled`，但 `pgrep -af` 列出了带 `-N -R`、绑定地址、远端端口、目标主机的裸 ssh 进程。因此只查进程；正则匹配该 argv 结构，约束端口边界并转义主机正则字符。`[s]sh` 避免匹配健康检查 shell 自身的命令文本。部署字段仅为 environment 占位符，未配置先 WAITING。

### tunnel-usvps

原文（地址和部署 home 脱敏）：

```text
systemctl is-active muse-selfheal-tunnel-usvps.service and pgrep -f ssh.*-R.*<ADDRESS>
```

新文：

```bash
host=$(printf "%s" "$RELAY_HOST" | sed 's/[][\\.^$*+?(){}|]/\\&/g') || exit 1
pgrep -f "(^|/)[s]sh[[:space:]].*[[:space:]]-R[[:space:]]+[^[:space:]]*:${REVERSE_PORT}:[^[:space:]]*[[:space:]].*(@|[[:space:]])${host}([[:space:]]|$)" >/dev/null
```

任务证据：第二条裸 ssh 进程同样带 `-N -R` 和另一个目标主机；禁用的 unit 不能代表它的存活。采用与上一条相同的进程检查，每个服务独立配置 environment。隔离测试用真实 pgrep 验证匹配、错误端口/主机、进程消失；没有连接远端。

### approver

原文（地址和部署 home 脱敏）：

```text
approverctl status (desired stopped counts as satisfied; running requires worker alive + recent list success)
```

新文：

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

任务证据：status 输出单个 JSON 对象，含 worker_pid 和 evidence.last_list_success_at；控制命令只允许 root 执行。现在只调用一次 status，保留 stopped/disabled 满足的原规则，并用 kill -0 验证正 PID 存活。运行时要求成功时间在过去 120 秒内；无效或未来时间不通过。清单 user=root，路径取部署 environment。

### douyin-parse

原文（地址和部署 home 脱敏）：

```text
curl -fsS -m 3 http://<ADDRESS>:8765/healthz && systemctl is-active muse-selfheal-douyin-parse.service
```

新文：

```bash
curl -fsS -m 3 "$HEALTH_URL" && systemctl is-active --quiet "$UNIT"
```

任务已确认原语法基本合法。保留 curl 的 -fsS -m 3 及先 HTTP 后 unit 的检查顺序；URL/unit 移至 environment，systemctl 加 --quiet。

### douyin-clip-mcp

原文（地址和部署 home 脱敏）：

```text
curl -fsS -m 3 http://<ADDRESS>:8766/healthz && systemctl is-active muse-selfheal-douyin-clip-mcp.service
```

新文：

```bash
curl -fsS -m 3 "$HEALTH_URL" && systemctl is-active --quiet "$UNIT"
```

任务已确认原语法基本合法。与 parse 相同，保留自身 URL 对应的 curl 和 unit 双重检查；任一失败均不能 READY。

### gemini-keepalive

原文（地址和部署 home 脱敏）：

```text
systemctl is-active muse-selfheal-gemini-keepalive.service and tail -n 50 <DEPLOY_HOME>/muse-keepalive/logs/keepalive.log | grep -qE 'refresh ok|ready:'
```

新文：

```bash
systemctl is-active --quiet "$UNIT" || exit 1
[[ "$MAX_LOG_AGE_SEC" =~ ^[0-9]+$ ]] || exit 1
[ -f "$LOG" ] || exit 1
last_epoch=$(stat -c %Y "$LOG") || exit 1
now_epoch=$(date +%s) || exit 1
age=$((now_epoch - last_epoch))
[ "$age" -ge 0 ] && [ "$age" -le "$MAX_LOG_AGE_SEC" ]
```

任务证据：日志文件存在且带历史 mtime，仅 grep 历史成功不能证明当前存活。改为 unit active 加日志 mtime 新鲜度，stat/date 失败或未来时间均不通过。任务提供的空账号 exit 0、Result=success 只说明正常退出；running 目标下 inactive 仍 FAILED。stopped 目标下用 stopped_check 验证 unit 已加载且 inactive。mtime 表示最近写入，不证明业务刷新成功。

## 验收原始输出

### 1. M1 原夹具

命令：`python3 /home/ribon/workspace/selfheal-merge/fixture_m1.py impl/bin/health.sh`

```text
health execution contract: PASS
```

退出 0。

### 2. 控制器 shell 语法

`bash -n impl/bin/health.sh` 的 stdout/stderr 均为空，退出 0；随后打印的退出码：

```text
bash-n exit=0
```

### 3. JSON 与 schema

`jq -e . impl/config/services.muse02.json >/dev/null && echo "json ok"`：

```text
json ok
```

退出 0。jq 本身仅检查 JSON；第 5 项实际调用 health.sh，通过其 schema 验证并执行了全部七项，没有控制器错误退出 2。

### 4. 每条 health_check / stopped_check 的 bash -n

执行任务提供的逐条脚本，并额外以 bad 数量设置 Python 退出码。检查全部 7 条 health_check 和 4 条 stopped_check，原始输出：

```text
all health_check are valid shell
```

退出 0。

### 5. 本机完整扫描

```bash
mkdir -p /tmp/m2/state /tmp/m2/run
impl/bin/health.sh --services impl/config/services.muse02.json \
  --state-dir /tmp/m2/state --run-dir /tmp/m2/run --json
```

原始 JSON（也已原子写入 `/tmp/m2/state/status.json`）：

```json
{
  "generation": "unknown",
  "boot_id": "abd1db52-e9a0-46d1-96fb-a085767cef68",
  "config_version": "1",
  "checked_at": "2026-10-27T03:58:19Z",
  "last_success": "",
  "consecutive_failures": 1,
  "status": "FAILED",
  "components": [
    {
      "id": "local-sshd",
      "status": "FAILED",
      "reason": "nonzero"
    },
    {
      "id": "tunnel-krvps",
      "status": "WAITING",
      "reason": "waiting"
    },
    {
      "id": "tunnel-usvps",
      "status": "WAITING",
      "reason": "waiting"
    },
    {
      "id": "approver",
      "status": "FAILED",
      "reason": "nonzero"
    },
    {
      "id": "douyin-parse",
      "status": "FAILED",
      "reason": "nonzero"
    },
    {
      "id": "douyin-clip-mcp",
      "status": "FAILED",
      "reason": "nonzero"
    },
    {
      "id": "gemini-keepalive",
      "status": "FAILED",
      "reason": "nonzero"
    }
  ]
}
```

```text
exit=20
```

两条隧道仍为部署占位符，故 WAITING；其余 FAILED。该结果没有将未知或缺失服务判为 READY。

### 新增隔离回归

`python3 impl/tests/test_m2.py`：

```text
protocol: WAITING, failure precedence (both orders), STOPPED, metadata, atomic output PASS
manifest probes: exact listener, approver single call/live PID/freshness/stopped, keepalive freshness/design-stopped, HTTP and unit conjunction PASS
tunnels: real pgrep matches SSH-shaped argv, rejects wrong port/host and absent process PASS
M2 regression: PASS
```

退出 0。只操作临时文件、模拟命令和无网络的测试进程。

## 实现位置

以下行号指 `impl/bin/health.sh`：

- 28–33：协议版本与 prerequisites schema 校验。
- 50–140：复用隔离用户/环境、超时回收的探测函数；129–135：timeout/command_missing/nonzero 原因。
- 147–155：前置探测及 WAITING；160–169：FAILED 优先和整体退出 10/20。
- 157–159：协议 2 的 STOPPED/stopped，旧协议 READY 兼容。
- 163–164：每组件 reason 输出。
- 171–190：generation、boot_id、config_version、checked_at、last_success、consecutive_failures。
- 191：同目录临时文件原子替换 status.json；原有 run-dir 锁覆盖扫描和读写。

last_success 沿用旧实现：本轮整体 READY 更新，否则保留；连续 FAILED 遇 READY/WAITING 归零。config_version 沿用旧协议字符串类型。

## 任务描述中的错误与必要修正

1. **M1/STOPPED 冲突**：见首节。已用显式版本保留旧入口，不能无条件同时满足两种互斥标签。
2. **`and` 不是 Bash 语法错误**：在这些原命令里它只是 systemctl 的普通参数；ss/pgrep 不会作为第二个命令运行。bash -n 可以接受这种错误逻辑，所以硬门槛还必须配合语义测试。approver 的自然语言括号则确实不合法。
3. **“永远不通过”超出了实测范围**：证据证明原命令在给定环境退出 1、独立 ss filter 退出 255。原组合没有真正执行 ss；若其他同名 unit 恰好 active，systemctl 多参数查询的行为可能不同。应表述为“当前部署错误检查，且不具备预期监听语义”。没有重复远端实测。
4. **sshd 的建议 grep 不精确**：地址中的点作为正则字符、端口子串均可能误匹配。改为 ss 输出的本地地址列精确相等，并检查 ss 自身成功。
5. **approver 示例没有验证 worker 活着**：仅 PID 非空不足，新增正整数和 kill -0。PID 身份仍依赖 approverctl 契约。只做 `age <= 120` 还会接受未来时间，已加下界并拒绝无效日期。
6. **日志 mtime 不能证明业务成功**：它证明最近写入，错误日志也会刷新。按任务要求实现 active + mtime；需更强“真的在工作”语义时必须由服务提供成功心跳。本次未添加未获授权的服务侧改造。
7. **jq 验证不等于 schema 校验**：第 3 条命令仅解析 JSON；第 5 条 health.sh 调用才验证 schema。

未改 `_evidence/`、`references/`、`SKILL.md` 或任何 M1 夹具；未 commit、未 push，也未操作生产服务。
