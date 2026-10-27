# M2-B 影子扫描报告

在 muse02 上用清单驱动执行器对真实 7 个服务做只读影子扫描，与生产 `health.sh` 对比。
影子目录 `/home/hatch/selfheal-shadow/{bin,config,state,run}` 与生产完全隔离。

## 一、新旧判定差异

| 来源 | 结果 |
|---|---|
| 生产 `health.sh` | `READY`，9 组件：`volume` `config` `tunnels` `local-sshd` `tunnel-krvps` `tunnel-usvps` `approver` `douyin-parse` `douyin-clip-mcp` |
| 新执行器（影子） | `FAILED`，7 组件：6 READY + `gemini-keepalive` FAILED |

**生产报 READY 是假绿。** 它的 9 个组件里**不含 `gemini-keepalive`**，而该服务当时真实状态是
`failed` / `ExecMainStatus=20`。`services.json` 登记了 7 个服务，`health.sh` 只检查其中 6 个
（外加 3 个不在清单里的硬编码项），漏掉的那一个正好坏了。

`volume` / `config` / `tunnels` 三项是硬编码在 `health.sh` 里的检查，不在 `services.json` 中。
迁移时需要决定：把它们作为清单条目补进去，还是保留为执行器内置的前置检查。

## 二、影子扫描暴露的真实故障

`gemini-keepalive` 失败原因（journal 原文）：

```
[fatal] browser executable not usable: /root/gem/cache/camoufox/browsers/official/152.0.4-beta.30-5720d45b/camoufox-bin
```

muse02 在 02:39 被平台重置，`/root/gem` 在根分区，随重置消失。而 `config.json` 仍指向该路径。
修复过程中还发现第二处同类问题：

```
[fatal] cannot load playwright-core: Cannot find module '/root/gem/node_modules/playwright-core'
```

**根因：运行时依赖（浏览器二进制 + node_modules）装在根分区，重置后必然失效。**
持久卷上 `/home/hatch/camoufox` 是个空目录，说明之前从没把运行时真正落到持久卷。

修复方式：npm 10.9.4 + camoufox-js 0.13.0 + playwright-core 1.62.1 + camoufox 156.0.1-beta.39
全部装到 `/home/hatch/gem`，并把 `config.json` 的 `browser.executablePath` 和
`browser.playwrightCore` 都改指持久卷。

## 三、`desired_state` 语义问题

修复运行时后 unit 变 `inactive` / `Result=success` / `ExecMainStatus=0`（`accounts=0` 时
`runner.js:148` 正常 `process.exit(0)`），但检查仍报 FAILED，因为 `health_check` 硬性要求
`systemctl is-active` 为真。

**这是检查逻辑的问题，不是服务的问题。** 该服务在无账号时的设计终态就是「正常退出」。
改用 `desired_state: "stopped"` + `stopped_check` 表达：

```bash
state=$(systemctl show --property=ActiveState --value "$UNIT") || exit 1
[ "$state" = inactive ] || exit 1
res=$(systemctl show --property=Result --value "$UNIT") || exit 1
[ "$res" = success ] || exit 1
code=$(systemctl show --property=ExecMainStatus --value "$UNIT") || exit 1
[ "$code" = 0 ]
```

这样能区分「设计上该停且干净退出」（STOPPED，正常）和「该跑却挂了」（FAILED）。

**待办**：有账号时该服务应为 `desired_state: "running"`，其 `health_check` 也必须在
`accounts > 0` 的前提下才要求 `is-active`。这个条件切换目前需要人工改清单，
应改为从 `config.json` 的 `accounts` 长度动态判定。

## 四、修正后的验收结果

```
overall: READY  (exit=0)
  local-sshd           READY    ready
  tunnel-krvps         READY    ready
  tunnel-usvps         READY    ready
  approver             READY    ready
  douyin-parse         READY    ready
  douyin-clip-mcp      READY    ready
  gemini-keepalive     STOPPED  stopped
```

两个完整扫描周期：

| 项 | cycle 1 | cycle 2 |
|---|---|---|
| `checked_at` | 04:46:00Z | 04:46:04Z（推进 ✓）|
| `status` | FAILED | READY |
| `consecutive_failures` | 1 | 0（正确归零 ✓）|
| `generation` | 稳定 ✓ | |
| `config_version` | 一致 ✓ | |
| `last_success` | — | 已写入 ✓ |

## 五、隔离性证明

| 项 | 生产 | 影子 |
|---|---|---|
| `status.json` mtime | 04:46:18 | 04:46:04 |
| `health.sh` md5 | `ba9ea1ae…` | `54b849ae…` |

hook `muse-selfheal-check.json` 仍指向 `/home/hatch/selfheal/state/status.json`（生产），未被改动。
生产 `health.sh` 未被替换，`patrol-*` hook 未被触碰。

## 六、迁移前必须处理的问题

1. **`volume` / `config` / `tunnels` 三个硬编码检查如何进清单**：它们在 `health.sh` 里，不在
   `services.json` 里。新执行器只读清单，直接切换会丢这三项检查。
2. **`gemini-keepalive` 的 desired_state 需要随 `accounts` 长度动态切换**，不能写死。
3. **运行时依赖必须落持久卷**，这条要写进 skill 作为硬约束：任何服务引用的二进制、
   node_modules、缓存目录都不得在 `/` 或 `/tmp` 下。
4. **`install-keepalive.sh --start` 误报**（muse01 上发现）：它 `sleep 4` 后要求 `is-active`，
   但无账号时 unit 必然已正常退出 → 打印 `FAILED to start` 并 `exit 20`。应改判
   `Result=success` 而非 `is-active`。

## 七、未做的事

- 未替换生产 `health.sh`
- 未改动 `patrol-*` hook
- 未修改生产 `services.json`
- 未执行 M3（切换）
