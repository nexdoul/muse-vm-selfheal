# M6：unit 持久化与自举

本地实现完成。采用方案 A：keepalive 的显式 repair 调用持久卷 installer，再根据账号配置 start/stop。controller 启动前执行 bootstrap；独立的 `boot-ensure.sh` 可以在 controller unit 完全不存在时重建它并提交启动任务。没有连接目标 VM，也没有 commit/push。

## 使用与发布布局

发布时保留整个 `impl/`，不要再只复制 bin/config：

```text
/home/hatch/selfheal-releases/current -> <完整 release>
<完整 release>/impl/
  bin/{bootstrap,boot-ensure,controller-loop,health,reconcile}.sh
  lib/{bootstrap,reconcile-manifest}.py
  adapters/keepalive-repair.sh
  config/services.json                 # 渲染后的本站清单
  units/muse-selfheal-controller.service
  units/muse-selfheal-gemini-keepalive.service
  units/muse-xvfb-keepalive.service
  units/<清单里其他非 external unit 的实际名称>
```

这里 `<selfheal_root>` 是 release 的 `impl/`。bootstrap 默认从脚本所在根的 `units/` 读取，也支持显式 `--unit-dir`；不是从 `/etc` 反推模板。三份新增模板均在 `impl/units/`。keepalive 两份模板沿用已有本地持久卷证据的运行设置，包括 headed/X11、持久化缓存、退出码和重启策略。

部署者必须先渲染清单原有占位符及新增 `KEEPALIVE_ROOT`，并将每个非 external unit 的模板放齐。**清单已有 cf-probe，因此还必须提供以 `<CF_PROBE_UNIT>` 实际值命名的模板**，取自部署者维护的 cf-probe 安装来源；本次没有虚构其 unit 名称或凭据设置。只有本仓库三份模板而没有 cf-probe 模板时，全清单 bootstrap 将明确失败。keepalive installer 使用的持久卷模板与 release 内两份 keepalive 模板应同步发布，避免两个恢复入口来回覆盖不同版本。

发布前在目标部署流程执行 dry-run（本任务未执行远程部署）：

```bash
B=/home/hatch/selfheal-releases/current/impl
bash "$B/bin/bootstrap.sh" --services "$B/config/services.json" \
  --unit-dir "$B/units" --dry-run
```

`--dry-run` 校验清单和模板、比较文件、打印计划，不 mkdir、不写锁、不调用 systemctl。真实执行需要能写 system unit 的身份，生产 controller 为 root。`--system-unit-dir` 是隔离测试入口；测试同时替换 PATH 中的 systemctl，不能把它当成完整的 systemd 离线 root 选项。

## 实现行为与方案选择

- bootstrap 读取清单的 `unit`，兼容已有 `environment.UNIT`，加上 controller 自身；不扫描目录安装额外模板。重复登记去重，所有权冲突和非法名称拒绝执行。external 不要求模板、不安装、不启用。
- 安装前读全模板，缺失、未渲染或空模板显式报错。安装过程加锁、同目录临时文件原子替换，权限 0644；内容相同不替换。每个 unit 输出 installed / unchanged / skipped(external) / error。
- 仅有文件变化或上轮遗留 reload 意图时 daemon-reload；reload 失败保留标记，下一次即使内容相同也重试。只有未 enabled 的 unit 执行 `enable --no-reload`，之后复查 enabled。任何失败返回非零。bootstrap 不 start/restart。
- `gemini-keepalive` repair 经 `keepalive-repair.sh` 调 installer（无 `--start`），检查 accounts 必须是数组，空数组 stop，非空 start；start-limit-hit 先 reset-failed。健康复查仍要求完整的原有运行/停止条件。
- Xvfb 是明确登记的第 11 个服务，同样由 accounts 决定运行/停止，沿用默认 unit 修复。因此 bootstrap 无须越过白名单去追踪 Wants 依赖。外部服务集合没有变化。
- 不选 B：不给 `repair: null` 自动增加写权限，不让健康执行器承担全局安装；既有 external/null 的只观察契约继续成立。`reconcile.sh` 无须修改，其原有自定义 repair、超时和修复后复查直接消费方案 A。
- controller 在进入循环前 bootstrap；失败退出 2，由新 controller unit 的 Restart=on-failure、RestartSec=30 重试，不带着安装失败进入 reconcile。原 RELEASES/SELFHEAL/SITE 用法保留，新增 SERVICES 可显式指定渲染后的清单。

## missing_unit 的契约

两个 keepalive 条目的运行和停止检查先查询 LoadState。只有查询成功且值严格为 `not-found`，才在 health 内部协议 FD 3 写入 `missing_unit:` 并退出 1。health 将它映射为 FAILED/missing_unit，整体退出 20。loaded 但状态、退出码、日志新鲜度不满足时仍为 FAILED/nonzero；查询错误也为 nonzero。

普通 stdout/stderr 继续丢弃。标记须伴随普通失败；成功、环境错误、超时不会被此标记降级或误判。其他自定义检查不增加隐式 systemctl 查询，已有显式 exit 127 仍为 nonzero。此协议可供其他**明确执行 unit 检查**的条目采用，未修改的旧清单不会凭一个 unit 字段自动触发查询。

最初尝试自动补查 LoadState 时，M3 的 external/null/自定义 repair 三项测试发现新增 systemctl 调用；现已改用检查内协议，保留原 M3 测试和调用边界。

## controller unit 丢了，谁拉起它

**持久卷上的平台脚本调用 `boot-ensure.sh`，不依赖 controller 的 unit，也不依赖 started 标记。** 它先运行 bootstrap，再运行 `systemctl --no-block start muse-selfheal-controller.service`；失败返回非零，锁竞争退出 10。非阻塞 start 避免平台入口等待 controller 自己的 bootstrap。启动提交成功不等于业务健康，controller 的健康结果仍从 status.json 读取。

已有完整 release 和渲染清单时，可直接执行的恢复命令为：

```bash
bash /home/hatch/selfheal-releases/current/impl/bin/boot-ensure.sh
```

仅把 controller 设置为 enabled 不构成重置后的入口：enable 的链接也在易失 `/etc`。必须将上述命令接到持久卷的平台入口。下节给出部署方案；本任务没有注册或修改目标机 hook。

## init.sh 改造方案

推荐保留平台原 home-init 入口，同时新增一个**持久化、每 60 秒执行的重试入口**，调用改造后的 init.sh。重试入口完全不看 `/run/hatch-home-init/started`。这样原 hook 已经提前 touch started、init 缺失、脚本被杀、某一步失败，都不会封死本次开机的后续恢复。不要靠 init EXIT trap 删除 started：init 根本未执行或被 SIGKILL 时 trap 无效。

部署者先将旧 restore-all 的保留部分抽成 `workspace/setup/restore-retained.sh <component>` 四个幂等分支，每个分支真实失败返回非零，不允许只打印 WARN 后报告成功：

| 分支 | 保留内容及归属 |
| --- | --- |
| cf-probe | 原第 3 步的安装/配置恢复；沿用已处理凭据的 installer，后续运行修复由 reconcile 接管 |
| approver | 原 3b 的 Node/离线依赖准备；启动使用现有 approver-repair adapter，保留维护标记与进程身份检查，不复制原宽泛 pgrep 启动逻辑 |
| nginx | 原 5b 的 ensure（nginx、证书、condor）和去重后的每周同步任务；若仍使用 cron，保留必要 cron 包和守护进程恢复，避免依赖 watchdog 段顺带启动 cron |
| crypto-auto | 显式移出旧体系的依赖准备、配置/数据恢复、唯一任务入口与进度检查；现有任务书的 9 步列表没有给出其具体启动命令，部署者需从已有 Crypto_Auto 入口提取，不能因删 watchdog 而漏掉此职责 |

删去旧第 4 步写 watchdog crontab 的语句和第 6 步即时 watchdog。部署切换时还要精确移除**已经存在**的 watchdog cron 行，保留无关任务。旧第 5c keepalive 的安装/启动交给 bootstrap/reconcile，避免双写。hermes、sshd 继续按原决定禁用；持久化 hook 定义不需要恢复。

改造后的 `/home/hatch/init.sh` 可以采用以下调度主体。`restore-retained.sh` 是上述待拆分的部署产物，不是本轮已存在的文件。每个成功分支每次启动只做一次；失败分支下次轮询重试；某一步失败不会短路其余分支。controller 每轮确保入口存在，以便失败后持续收敛。

```bash
#!/bin/bash
set -uo pipefail
umask 077
mkdir -p /run/selfheal-init /home/hatch/selfheal/logs || exit 1
exec 9>/run/selfheal-init/lock
flock -n 9 || exit 10
exec >>/home/hatch/selfheal/logs/init.log 2>&1
rc=0
B=/home/hatch/selfheal-releases/current/impl
# 与旧 restore-all 是否存在无关；先启动新框架。
timeout -k 5 120 bash "$B/bin/boot-ensure.sh" 9>&- || rc=1
for component in cf-probe approver nginx crypto-auto; do
    done_file="/run/selfheal-init/$component.done"
    [[ -e $done_file ]] && continue
    if timeout -k 5 90 bash /home/hatch/workspace/setup/restore-retained.sh \
        "$component" 9>&-; then
        touch "$done_file" || rc=1
    else
        printf '%s: restore failed; retry next poll\n' "$component"
        rc=1
    fi
done
exit "$rc"
```

将独立重试 hook 的脚本放在持久卷 `hooks/scripts/selfheal-init-retry.sh`（与 init 一样 root 所有，只允许受信任用户修改）：

```bash
#!/bin/bash
set -u
source "${HATCH_HOOK_RUNTIME:?}"
[[ "${HATCH_HOOK_DRY_RUN:-0}" == 1 ]] && silent "dry-run"
[[ -x /home/hatch/init.sh ]] || silent "waiting for init.sh"
mkdir -p /home/hatch/selfheal/logs || silent "cannot create init log directory"
timeout -k 5 540 /bin/bash /home/hatch/init.sh \
    >>/home/hatch/selfheal/logs/init-retry.log 2>&1
rc=$?
silent "selfheal init attempt rc=$rc; retry on next poll"
```

对应 `hooks/definitions/selfheal-init-retry.json`：

```json
{
  "version": 1,
  "id": "selfheal-init-retry",
  "enabled": true,
  "poll_interval_secs": 60,
  "script_timeout_secs": 600,
  "script_path": "/home/hatch/hooks/scripts/selfheal-init-retry.sh",
  "prompt": "Run the persistent initialization retry script.",
  "delivery": {"surface": "main"},
  "created_at_ms": 0,
  "updated_at_ms": 0
}
```

先准备完整 release、保留分支和新 init，再切换入口、注册重试 hook；否则每分钟调用旧 init 会重新安装 watchdog。现有 started 即使已存在也无须删除。迁移后该 hook 可持续运行，init 自己的 flock 防止与 home-init 并发；所有子命令关闭锁 FD，避免 daemon 继承锁。同步阶段有时间上限，失败不中断其他组件；平台 hook 单独执行，不阻塞其他平台巡检。

另一台没有 init/setup 的站点只需将同样的持久化重试入口接到其完整 release 的 `boot-ensure.sh`，不要复制这台机器的旧组件恢复职责，也不要假设那里的 controller unit 已持久化。

## 其他易失状态清单

以下是发布/迁移时需明确处理的清单，不是本轮新增远程实测：

| 位置/状态 | 持久化或重建办法 |
| --- | --- |
| `/etc/systemd/system` 的 unit、drop-in、enable 链接 | 模板和覆盖设置进持久卷；重建 unit/链接，reload；不能只备份 enabled 状态 |
| `/etc` 的服务配置、秘密、证书、SSH 配置 | 权威版本放权限受控持久卷；原子恢复或受控链接；外部 SSH/hermes 仍由其所有者负责 |
| `/var/spool/cron` 的 crontab、`/etc/cron*` | 仅保留任务的声明进持久卷，幂等重建；不恢复 watchdog |
| `/var/lib` 的业务数据库、任务游标、浏览器登录态 | 数据及一致性备份迁到持久卷；不要恢复陈旧 PID/锁或把 dpkg 数据库单独搬走 |
| `/var/log`、journal 中唯一诊断证据 | controller/恢复日志写持久卷并轮转；需要跨重置审计的其他日志持久化或外送 |
| 易失根内的软件包、`/usr/local/bin` 二进制、依赖缓存 | 固定版本安装声明、离线包和安装器留持久卷；用包管理重装，不只复制包数据库 |
| `/run` 的 PID、socket、flock、开机成功标记 | 每次启动重新创建，不作为跨重置权威状态；成功标记在重置后消失正是所需行为 |
| 框架 state/config/current 指针及迁移回滚记录 | 保留在持久卷；运行锁由 flock 持有，锁文件存在本身不代表已有活进程 |

## 对任务描述的补充与校正

核心平台事实按任务书采信，没有重复调查。需要校正的是恢复链条的充分性：

1. 只加 installer repair 不足以恢复 running 目标，因为 installer 默认不启动；实现已补齐按账号 start/stop。
2. “controller 启动时 bootstrap”只能修复已被拉起后的环境，不能解释首次启动；必须有持久化平台入口。
3. Xvfb 必须登记才能被白名单 bootstrap 安装，不能只把模板放进目录；本次明确增加该条目。
4. 改 init 的 exit code 不能让已有 home-init 重试；独立重试入口避开 started，且处理 init 不存在的情况。
5. 给出的 restore-all 步骤未包含 Crypto_Auto 的具体恢复命令，不能宣称删除 watchdog 后自然保留了它；上面的拆分将它列为必须保留的独立分支。

## 验证与兼容性

验证命令和实际输出见 [M6_VALIDATION.txt](M6_VALIDATION.txt)。六套既有回归全部通过，新增 M6 测试覆盖幂等 inode/mtime、dry-run 无写入、白名单/external、缺模板、reload 失败后重试、enable 失败、模拟目录清空、controller 启动顺序、独立启动入口、missing_unit、空/非空账号恢复及旧失败协议。

M4 的三处契约断言随明确需求更新：服务数精确改为 11；计划精确加入 Xvfb 和 Gemini；`repair is None` 改为精确断言修复命令，并补齐新占位符夹具。没有删除测试、改成宽松包含判断或削弱健康校验。M1/M2/M2C/M3/M5 测试文件未改。

反证在临时副本把内容相同比较改成总为 false；幂等测试因 inode/mtime 改变失败，反证驱动确认成功捕获，原实现保持不变。systemd-analyze verify 检查模板，未启动真实服务。真实重置与平台入口验收留在后续部署阶段；本轮交付的是本地实现和 init 迁移方案。
