# 迁移与验收命令

用于 SKILL.md 第 9 节的 M0–M6。muse01/muse02 是来源方案标签，不是部署主机名。所有尖括号在目标机私有操作记录中替换；本仓库只交付契约，不执行 VM 迁移。

## 恢复与迁移的区别

本文件描述 M0–M6 迁移。执行前先确认当前请求属于哪一类，不要因为本文件要求逐阶段授权，就让已登记的服务在重置后停摆等待授权。

| 活动 | 判据 | 授权要求 |
| --- | --- | --- |
| 只读调查 | 只列出现有入口、周期、所有者与状态 | 无需授权 |
| 恢复已登记服务（recovery） | 责任清单条目集合与所有者映射在操作前后不变：重建被重置清空的同一 unit、重跑幂等安装器、重启同一进程、重连同一隧道 | 常规运维，**无需逐阶段授权** |
| 迁移（migration） | 新增/删除/改名组件、改变 `ownership`、更换框架、改 hook/cron 入口与周期、改清单结构 | 必须逐阶段明确授权 |

recovery 仍须满足：只操作清单内已有条目；幂等可重入；不改所有者与责任边界；恢复后验证 `is-enabled` + `is-active`（或设计终态，如等待人工登录的 `rc=10`）+ 实际端口监听/进程身份 + `NRestarts` 无异常增长；失败显式报错并保持 FAILED/20，不假装成功。重置后清单内的服务未自动恢复是缺陷，不是正常状态；此时先按 recovery 处理，再记录缺陷，不进入 M0–M6 授权流程。

**迁移执行护栏：** M0–M6 是独立活动，必须由用户逐阶段明确授权，读取本 Skill 不构成迁移授权。只读调查（列出现有入口、周期、所有者）无需额外授权；除上节 recovery 边界内的恢复动作外，任何写操作，包括准备快照、停 cron、改 hook、换 unit、安装或卸载组件，都必须先获得该阶段的明确授权。用户仅要求“更新配置”或“了解现状”时，禁止执行 M0–M6 的任何切换动作；未获迁移授权时交付“现状清单 + 待执行动作列表”，不得声称迁移已完成。M1 验收所需的 `--services/--state-dir/--run-dir/--json` 接口在当前已部署实现中不存在，必须先实现再验收，不能把文档中的命令当成系统已支持的能力。

## 目标接口先实现，再执行验收

实现者在新版本目录生成下列接口，先用 `--help` 核对支持范围。旧证据脚本没有这些参数，不得直接拿旧脚本跑夹具：

| 接口 | 必须行为 |
| --- | --- |
| `health.sh --services FILE --state-dir DIR --run-dir DIR --json` | 使用指定清单、状态和运行目录；JSON 写 stdout，诊断写 stderr；原子保存 `DIR/status.json`；不触发 bootstrap/reconcile。隔离模式只读所给服务，不额外探测未在清单声明的生产卷或 unit；生产默认模式额外检查真实卷/控制器 |
| `reconcile.sh --validate --services FILE` | 只解析 schema、shell 语法、依赖环、字段引用和路径，不启动/停止服务，不执行探测；成功 0，清单非法 20 |
| `reconcile.sh --plan --services FILE` | 只输出脱敏动作计划，禁止调用准备脚本、安装器、真实 repair 或审批 |
| `bootstrap.sh --ensure` | 使用固定 site 配置，遵守所有者和维护状态，只重建基础入口，不硬编码业务启动 |
| `migration/{quiesce-old,enable-new,quiesce-new,restore-old}.sh` | M0 时按本机实际调度 API 生成的幂等移交脚本；准确停用注册并等待在途修复退出，失败非零，绝不整体清空 cron。restore-old 使用独立保存的代码/注册材料，不依赖新 bootstrap |
| `migration/audit-owners.sh` | 读取实际平台注册、相关账户 cron、timer/unit 及进程身份；按 ownership 清单检查每个组件恰一个写调度者，external 不被新控制器写入；不只数脚本文件；读取当前移交记录，支持新旧两种所有者映射 |

`quiesce-old` 必须连同旧 home-init/恢复入口及故障唤醒 prompt 一起处理，防止重置或延迟 agent 任务重新装回旧 watchdog。记录平台更新回执并重读实际注册；修改 definitions JSON 文件不等于完成停用。

## M0：两侧冻结与回滚准备

用目标部署账户执行以下命令；需要的系统权限沿用现有授权。参数放私有记录，不写进 skill：

```bash
NEW='<NEW_RELEASE_DIR>'
OLD='<OLD_RELEASE_DIR>'
ROLLBACK='<PRIVATE_ROLLBACK_DIR>'
SUPERVISOR_UNIT='<SUPERVISOR_UNIT>'
mkdir -p "$ROLLBACK"
chmod 700 "$ROLLBACK"
umask 077
# 确认目标账户确有 crontab；无 crontab 时在记录中明确标注，不吞其他错误。
crontab -l > "$ROLLBACK/crontab.before"
cp -a "$OLD" "$ROLLBACK/release.before"
systemctl show "$SUPERVISOR_UNIT" -p ActiveState -p UnitFileState > "$ROLLBACK/supervisor.before"
```

对于尚无 supervisor unit 的脚本型部署，将该查询记为“不存在”，不安装 unit 来满足 M0。快照包含本次接管范围的 unit、hook 实际注册导出、旧状态/策略和调用关系；秘密仅保存在受限回滚介质。登记备份时刻，恢复代码时排除业务数据、秘密和新产生的会话。各相关 cron 账户分别导出。用平台现有 CLI/API 导出 hook，不杜撰统一平台命令；将实际调用写入移交脚本。从独立平台通道或第二条已验证连接运行 `true`，确认回滚不依赖将被替换的隧道。

验收：`test -s "$ROLLBACK/crontab.before"` 适用于原有非空 cron 的部署；对原无任务的部署检查记录的空状态。`test -d "$ROLLBACK/release.before"`，并逐个读取注册导出。此阶段失败只停止准备，不改旧调度。

## M1：先在 muse02 型实现上验证清单执行

先转换所有旧检查：`and` 改为真实 shell 组合；自然语言改成可执行适配器；隧道检查使用真实执行后端，不能检查按设计禁用的 unit；浏览器保活检查用本轮功能时间而非历史日志关键词。审批适配器只读统一状态，同时校验列表成功、决策错误和新鲜度。`fix=reconcile.sh` 改成组件入口，防止递归。

```bash
find "$NEW/bin" -type f -name '*.sh' -exec bash -n {} \;
"$NEW/bin/reconcile.sh" --validate --services "$NEW/config/services.json"
"$NEW/bin/reconcile.sh" --plan --services "$NEW/config/services.json"
```

以下 Python 夹具在本机临时目录创建任意新 ID，调用新执行器；只使用 `exit`、`sleep`、环境断言，不接触真实服务。退出断言失败就是 M1 未通过；不要修改测试预期来容纳硬编码结果。

```bash
python3 - "$NEW/bin/health.sh" <<'PY'
import json, os, pathlib, pwd, subprocess, sys, tempfile
health = str(pathlib.Path(sys.argv[1]).resolve())
with tempfile.TemporaryDirectory(prefix='health-contract-') as d:
    root = pathlib.Path(d)
    (root/'state').mkdir(); (root/'run').mkdir()
    envfile = root/'probe.env'
    envfile.write_text('PROBE_VALUE=from-file\n'); envfile.chmod(0o600)
    services = root/'services.json'
    base = dict(id='arbitrary-new-service', kind='fixture', ownership='managed',
                desired_state='running', user=pwd.getpwuid(os.getuid()).pw_name, home=d, workdir=d,
                environment={}, environment_file=str(envfile), dependencies=[],
                health_timeout_sec=1, startup_grace_sec=0, timeout_sec=5,
                retry_budget=0, retry_backoff_sec=1, execution_context='local',
                start=['/bin/true'], stop=['/bin/true'], repair=['/bin/true'],
                stopped_check='exit 0')
    def run(cmd, expected, reason=None, **overrides):
        svc = dict(base, health_check=cmd, **overrides)
        services.write_text(json.dumps(dict(version=1, services=[svc])))
        env = dict(os.environ, HOME='/nonexistent-test-home', PROBE_LEAK='bad')
        p = subprocess.run([health, '--services', str(services), '--state-dir',
                            str(root/'state'), '--run-dir', str(root/'run'), '--json'],
                           env=env, capture_output=True, text=True, timeout=8)
        assert p.returncode == expected, (cmd, p.returncode)
        out = json.loads(p.stdout)
        rows = [c for c in out['components'] if c['id'] == svc['id']]
        assert len(rows) == 1, out
        c = rows[0]
        assert c['status'] == ('READY' if expected == 0 else 'FAILED'), c
        if reason: assert c['reason'] == reason, c
        return out
    run('exit 0', 0)
    run('exit 7', 20, 'nonzero')
    run('exit 10', 20, 'nonzero')
    run('exit 20', 20, 'nonzero')
    run('sleep 30', 20, 'timeout')
    run('__selfheal_nonexistent_command__', 20, 'command_missing')
    run('printf "not-json\\n"; printf "diagnostic\\n" >&2', 0)
    run('test "$PWD" = "$HOME" && test "$PROBE_VALUE" = from-file && '
        'test -z "${PROBE_LEAK+x}" && test "$(id -un)" = "$USER"', 0)
    # stopped 必须跳过 running 探测；由停止检查证明目标满足。
    run('exit 7', 0, desired_state='stopped')
    run('exit 0', 20, 'nonzero', desired_state='stopped', stopped_check='exit 9')
    run('exit 0', 0, id='second-new-id')
    result = json.loads((root/'state/status.json').read_text())
    assert not any(c['id'] == base['id'] for c in result['components']), result
print('health execution contract: PASS')
PY
```

同阶段补充夹具（由实现者针对实际锁/时钟接口生成，不操作生产）：并发两个修复只执行一次；把锁 PID 指向一个无关 `sleep` 进程且 starttime 不符，验证无关进程存活；已活跃锁超时不能并行进入；失败条目后接 WAITING 条目仍退出 20；超时子进程确已回收；依赖环拒绝；维护状态不拉起；预算耗尽不永久 10；旧 generation 不刷新成功。`HOME` 异常的 bootstrap 测试使用隔离 site 配置，确认只定位声明根目录。

每项测试必须产生退出码和断言记录，缺任何一项不进入 M2。M1 失败保留旧版在役，修改新版本执行器/清单，不改生产。

## M2–M3：muse02 型先切换

M1 完成后，统一解析 `desired_state_ref`；审批停止标记优先，sshd 准备放其适配器，agent 隧道仍由原授权上下文执行，删除核心中的具体 ID 分支。卷身份和所有路径来自 site；保留已确认审批策略及 VM 绑定。检查单轮预算，不能继续用 15 秒总扫描上限。

只读影子扫描使用独立 state/run 目录，检查适配器只读；不得运行旧 `patrol-*` 的 dry-run 代替影子扫描，因为旧脚本仍会执行修复。

```bash
SHADOW='<PRIVATE_SHADOW_DIR>'
mkdir -p "$SHADOW/state" "$SHADOW/run"
"$NEW/bin/health.sh" --services "$NEW/config/services.json" \
  --state-dir "$SHADOW/state" --run-dir "$SHADOW/run" --json > "$SHADOW/result.json"
# 必须恰好覆盖清单 ID，不能因为新增服务漏检而报绿。
jq -e --slurpfile cfg "$NEW/config/services.json" \
  '([.components[].id] | sort) == ([$cfg[0].services[].id] | sort)' "$SHADOW/result.json"
```

若业务确有故障，非零检查是有效证据，但必须修复或明确保留原维护目标后再切换；不得为迁移临时删除坏服务。`systemd-analyze verify "$NEW"/units/*.service` 检查目标机 unit，现场修复真实依赖环和不支持字段，不把普通警告直接当功能验收。

```bash
# 在子 shell 中停止失败步骤，不改变操作者后续 shell 选项。
(
set -e
"$NEW/migration/quiesce-old.sh"
"$NEW/migration/enable-new.sh"
"$NEW/migration/audit-owners.sh"
"$NEW/bin/bootstrap.sh" --ensure
"$NEW/bin/health.sh" --json > "$SHADOW/live.json"
jq -e '.status == "READY"' "$SHADOW/live.json"
systemctl is-active --quiet "$SUPERVISOR_UNIT"
)
```

验收至少两个完整扫描周期：两份状态的 checked_at 推进、generation 稳定、配置版本一致，实际功能通过；hook 调度记录有调用且正常轮次无模型 worker。停止/恢复测试用专用假服务，真实维护组件保持原状态。enable-new 必须切换脚本、清单、适配器及 unit 引用到同一版本；quiesce-new 同时禁用会重建新控制器的 hook/bootstrap 调用，不能只 stop supervisor 让 hook 下一轮再拉起。扫描出现 10 时先等预算内恢复，不将 10 当最终验收通过。

M2 失败不切换；M3 失败运行本文回滚顺序。新的扫描器、清单和适配器作为一个版本整体回滚，不能只回滚脚本留下新的命令语义。

## M4：muse01 型职责迁入

先满足 M3，再在另一部署独立准备版本与回滚材料，不复制 site/凭据/账户。登记 cf-probe 型 agent、证书同步、网关与 condor 型后端；SSH 独立体系和用户钉版本自愈保持 external，保留其维护入口。不能把“当前只运行一个自建服务”误解为只有一个恢复职责。

验收命令中的变量均指目标机私有配置解析出的路径/名称：

```bash
# 二进制可运行只是安装层验收；使用其实际支持的本地版本/帮助命令。
test -x "$AGENT_BIN"
timeout 10 "$AGENT_BIN" --help >/dev/null
systemctl is-active --quiet "$AGENT_UNIT"
# 单一秘密文件权限；只查元数据，禁止 cat 或打印 argv 中的秘密。
test "$(stat -c %a "$AGENT_SECRET_FILE")" = 600
# 以下适配器由部署实现者按第 6 节生成，读取固定私有配置，不接收秘密参数。
"$NEW/bin/check-agent-report.sh"
openssl x509 -in "$CERT" -noout -checkend 0
openssl x509 -in "$CERT" -noout -checkend 604800
nginx -t
```

`check-agent-report.sh` 必须验证服务端该 agent 的接受时间和样本推进，缺证据非零。预警检查非零表示临近到期，不能据此宣称已经过期。单独核对 notBefore、密钥匹配、SAN 和链。

```bash
# timeout 防止握手挂住；必须打开主机名和信任链验证。
timeout 10 openssl s_client -connect "$TLS_ENDPOINT" -servername "$TLS_NAME" \
  -verify_hostname "$TLS_NAME" -verify_return_error -CAfile "$CA_BUNDLE" \
  </dev/null > "$SHADOW/tls.txt" 2> "$SHADOW/tls.err"
openssl x509 -in "$SHADOW/tls.txt" -out "$SHADOW/served.pem"
openssl x509 -in "$CERT" -noout -serial -dates -pubkey > "$SHADOW/expected-cert.txt"
openssl x509 -in "$SHADOW/served.pem" -noout -serial -dates -pubkey > "$SHADOW/served-cert.txt"
cmp "$SHADOW/expected-cert.txt" "$SHADOW/served-cert.txt"
curl --fail --silent --show-error --max-time 10 --cacert "$CA_BUNDLE" "$HTTPS_HEALTH_URL" >/dev/null
```

离线/同步故障测试只在隔离目录和专用测试 unit/端口进行：禁用测试环境出网，验证安装器下载失败仍能从缓存二进制启动；假服务端不可达时 local_ready 保留且 report_ready 非成功；远端同步失败＋有效本地证书仍启动；过期/未来生效/缺失/密钥不匹配证书拒绝启动；不完整同步不覆盖当前版本；reload 失败回退有效旧版本。适配器必须接收测试配置文件，测试不能改生产证书或全机防火墙。

每种场景记录实际命令、退出码及证书/服务状态断言。缺缓存、秘密输入接口或上报查询接口时，M4 对应组件不放行，继续保留旧所有者；其他独立组件能够单独移交。

## M5：muse01 型切换与清理

沿用 M3 的严格移交命令，但 quiesce-old 必须逐项覆盖：

- `watchdog.sh` 的分钟 cron、`restore-all.sh` 的调用/重注册路径、旧 keepalive 恢复 prompt。
- `patrol-tunnel-watchdog` 与 `patrol-ssh-reconcile` 对同一隧道的重复调用：保留用户独立 SSH 所有者的唯一调度入口，停重复项；新框架只观察 external，不新开隧道修复循环。
- 旧证书同步 cron 在新调度接管前停用并排空，同步职责再移交；后台进程由唯一监督器管理。
- `home-init` 若还负责其他初始化，只移走已迁移自愈调用；用户独立 Hermes hook 保持原所有者，不为“只剩一个 hook”而删除无关职责。

验收用 `audit-owners.sh` 加平台注册 readback，至少观察两个周期无旧 restore/watchdog 重新创建。新入口正常后才归档历史清理候选；不得直接运行旧 restore-all 回滚，因为它会重装 cron 并重新带回重复巡检。

BRIEF 的 640 行是退役候选规模，不是无条件删除授权。`run-probe-install.sh` 仍被 restore-all/watchdog 引用，`cf-probe-install.saved.sh` 仍是旧安装器兜底：先替换调用并证明新离线路径可用，才移入受限归档。旧 Hermes 恢复/备份代码不进入新框架；独立用户自愈不在清理范围内。

```bash
# 只输出引用文件名，不输出旧安装器可能包含的秘密内容。
rg -l 'run-probe-install|cf-probe-install\.saved|restore-hermes|backup-hermes|restore-all|watchdog\.sh' \
  "$NEW/bin" "$NEW/units" "$NEW/config"
"$NEW/migration/audit-owners.sh"
```

新活跃执行路径不得再引用退役脚本；rg 的无匹配退出 1 是此项预期，不能拿它判扫描器失败。归档保留至自然重置验收和约定回滚窗口结束，不在本次文档编辑中删除任何证据。

## 回滚与 M6 自然重置验收

任一生产切换失败，按下列顺序执行，每步失败立即停止并使用独立通道处理，禁止并行开旧入口：

```bash
(
set -e
"$NEW/migration/quiesce-new.sh"
"$NEW/migration/restore-old.sh"
"$NEW/migration/audit-owners.sh"
)
```

restore-old 恢复 M0 记录的旧版本、unit 和选定调度，保留当前业务数据、秘密及维护决定；audit 在回滚后按旧所有者映射验收。对 muse01 只恢复预先选定的一个修复调度入口，不无差别导入原来互相重叠的整套任务。证书回滚前重新检查有效期；旧证书已过期则报告 TLS 阻塞，不能伪造成功。回滚对所有组件都失败时停在无自动写入口状态，保留健康业务与独立连接，由操作者修复入口。

自然重置后用生产 `bootstrap.sh --ensure`、`health.sh --json`、实际 SSH/HTTPS/上报检查及 `audit-owners.sh` 补验。确认 HOME 被纠正为声明值、unit 从持久材料重建、维护状态未变、没有联网下载主路径、长任务从检查点继续。记录首次平台可执行、触发、服务就绪和业务恢复时间；未经历自然重置前该项保持待验证，不主动重置生产充数。
