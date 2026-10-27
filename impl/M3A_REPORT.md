# M3-A：通用 reconcile 与本地切换模板

实现入口为 `bin/reconcile.sh`，完整迁移清单为 `config/services.muse02.reconcile.json`。
三份切换入口位于 `migration/`；使用方法与 release 布局见 `migration/README.md`。
全部验收命令、退出码、dry-run 和反证的原始输出见 [M3A_VALIDATION.txt](M3A_VALIDATION.txt)。

## 1. reconcile 的关键改写

原实现直接读字面量，并根据服务 ID/kind 分支：

```bash
desired="$(printf '%s' "$svc" | jq -r '.desired_state')"
if [ "$id" = "approver" ]; then ...; fi
if [ "$id" = "local-sshd" ]; then ...; fi
if [ "$kind" = "ssh-reverse-tunnel" ]; then ...; fi
```

现在先校验并解析所有目标引用，再在通用循环内根据健康结果和修复权限决策。
`lib/reconcile-manifest.py` 实现与 health 一致的 JSON Pointer/数组映射语义；
`health.sh` 本身及其已有行为未改。prepare/repair 复用 health 的隔离环境、用户切换和进程组超时。

```bash
manifest=$(python3 "$base/lib/reconcile-manifest.py" resolve "$services") || exit 2
# ... each service ...
if jq -e '.ownership == "external" or (has("repair") and .repair == null)' <<<"$service" >/dev/null; then
    overall=20; continue
fi
```

`repair` 字符串/数组执行声明命令；缺省才使用 `unit` 或 `environment.UNIT` 的 start/stop。
`--validate` 只校验 schema；`--plan` 只解析配置并输出**带条件**的动作数组，不运行检查、修复或 systemctl。
计划的条件是目标检查失败，且 running 的前置条件通过；实际执行才判断这些条件。
两个只读模式均不创建 state/run 目录。锁竞争返回 10。

原 WAITING 分支直接 `overall=10`；现在所有 WAITING 合并点均保护 FAILED：

```bash
10) [[ $overall == 20 ]] || overall=10; continue;;
```

默认 unit 修复查询 Result，命中 start-limit-hit 先 reset-failed，再 start/stop。
修复后 sleep（默认 3 秒）并复查，接受的 running 终态为：

```bash
[[ $state == active || ( $state == inactive && $result == success && $code == 0 ) ]]
```

停止目标必须为后一种干净终态。随后还要通过声明的目标检查；自定义 repair 也必须经过 sleep
和目标复查。干净退出不会绕过日志新鲜度、HTTP 等业务条件。

最终调用传入同样的路径，合并健康退出码而不覆盖已经发生的修复失败：

```bash
args=(--services "$services" --state-dir "$state_dir" --run-dir "$run_dir")
$json && args+=(--json)
"$base/bin/health.sh" "${args[@]}"; health_rc=$?
case $health_rc in
    0) ;;
    10) [[ $overall == 20 ]] || overall=10;;
    20) overall=20;;
    *) exit "$health_rc";;
esac
exit "$overall"
```

stdout/status.json 是最终 health 结果；退出码同时包含本轮修复结果。
修复命令失败但其副作用已经使健康检查通过时，health 可以 READY，而 reconcile 仍返回 20。
这保留“任一失败优先”的语义，调用者必须检查退出码。

## 2. 三条特殊逻辑的清单表达

| 原特例 | 新清单声明 |
|---|---|
| approver 外部配置权威 | `desired_state_ref: {file:"<APPROVER_CONFIG>", pointer:"/desired_state"}`；停止检查查 worker_pid；running 前置条件检查 enabled 和停止标记；unit 修复不改配置、不清停止标记 |
| local-sshd 准备 | `prepare: ["\"$PREPARE\""]`，PREPARE 指向部署中的准备适配器 |
| 两条隧道 | 保留描述性 kind；`repair:null`、`ownership:external`，使用已有 prerequisites 和 health_check 观察配置/进程 |

新模板仍有十项（3 infrastructure + 7 服务）。核心没有任何具体服务名，也不按 kind 分支。
原 `services.muse02.json` 保留：M2 的审批夹具只替换 environment.APPROVER_CONFIG，
不会替换一个新添的 desired_state_ref.file。直接改原模板会破坏已有入口和夹具的配置契约。
因此用独立的完整 reconcile 模板承载新声明，不改老测试迁就实现。部署时将新模板渲染进
release/config/services.json；审批引用路径必须与 APPROVER_CONFIG 相同。

## 3. 三个切换入口

```text
quiesce-old.sh --root DIR --unit OLD_UNIT --hook ENTRY [--hook ENTRY ...] [--dry-run]
enable-new.sh --root DIR --release RELEASE --services FILE --unit NEW_UNIT
              --unit-link PATH --hook-link PATH [--dry-run]
rollback.sh --root DIR [--dry-run]
```

- quiesce 在修改前记录 active/enabled 状态，把全部声明的 hook/bootstrap 入口移到私有存档，
  再停止并禁用旧 unit。重复调用不会覆盖原始状态。
- enable 预检完整 release（bin/lib/config/adapters/units/hooks），用单一 current 指针切版本；
  unit/hook 引用通过同一指针。要求 health 退出 0、READY、组件 ID 多重集合完全匹配，并验证
  新 unit 活跃后才接通新 hook。预检及切换失败均自动恢复已停用的旧控制器。
- rollback 先关闭新 hook 和 unit，再整体恢复版本/unit 引用，恢复旧入口和记录的运行/启用状态。
  回滚幂等；失败状态的旧 unit 保持停止，不主动制造故障来复现历史 failed 状态。

真实模式要求新的 unit 名和引用路径空闲。hook 参数须涵盖全部专用重启入口；共享配置中的
内联调用须先拆出专用入口，并等在途旧调用结束。模板不修改 unit 内的部署路径，打包者须让
unit、hook 与适配器指向同一 release 或 ROOT/current。所有真实 systemd/目标机运行验收留在部署阶段。
本次只执行临时目录内的假 systemctl 测试及 dry-run，没有操作实际服务或远程机器。

dry-run 原始输出在验收日志中，包含任务指定的两个简短命令和 rollback 演示。

## 4. 验证与反证

`test_m3.py` 覆盖 schema、无副作用计划、repair:null、external、两种排列的失败优先级、
reset-failed/start 顺序、干净退出与失败终态、prepare/repair 顺序和复查、引用停止目标、锁、
静态检查，以及切换成功/自动回滚/预检失败恢复/重复调用/dry-run。

反证只改临时副本，通过环境变量指定被测脚本，不修改仓库源码：

| 故意破坏 | 测试抓到的行为 |
|---|---|
| 删除 repair:null 执行保护 | 不该执行的 prepare 写出标记，`assertFalse(marker.exists())` 失败 |
| 让 WAITING 无条件覆盖 FAILED | 修复失败被最终 WAITING 掩盖，实际退出 10 而应为 20 |
| 删除 unit 设计终态保护 | health 变绿但 unit 仍 failed/脏退出，实际退出 0 而应为 20 |

优先级测试特意让失败修复的副作用使最终 health 仅为 WAITING，避免 health 自身的正确实现
掩盖 reconcile 的错误。终态独立测试也让 health 在 start 后变绿，确保失败确实由终态保护抓住。
反证全部检测成功后，再对原实现执行完整测试。

第一次汇总验收中，M1/M2/M2C 和语法检查通过，但随后 M3 曾出现 WAITING 覆盖 FAILED、以及
读取脚本的瞬时 EOF 错误。重新读取时源码已恢复正确内容，文件修改时间发生变化；具体写入者未确定。
该次失败完整保留在日志中，不作为通过证据；日志后半段记录针对当前源码的定点复测、完整复测及反证。

## 5. 任务说明需要纠正之处

1. “五个缺陷”的表格实际有六项。
2. 旧脚本读取缺失 desired_state 得到 `null`，它既不等于 running，也不等于 stopped。
   因此给只有 desired_state_ref 的条目不会“当 running 反复 start”，而是漏掉按引用目标修复。
   不支持引用确实是缺陷，但所述触发机制不成立。
3. `bash -n impl/migration/*.sh` 只语法检查第一个展开的文件，其他文件成为位置参数。
   除原命令外已逐个运行 bash -n。
4. `--plan` 若必须不调用 systemctl，就不能保证获知实时 systemd 健康状态。
   本实现明确输出条件动作计划，不把清单候选动作冒充已确认需要执行的动作。
5. “最后取 health 退出码”若理解为无条件覆盖，会丢掉修复失败。沿用旧实现的合并意图，
   FAILED 优先，并在文档说明健康 JSON 与修复总退出码的区别。

未修改 `_evidence/`、`references/`、fixture_m1.py、health.sh 或既有测试；未 commit、未 push。

## 交付时发现的并发 Git 变更

任务开始 HEAD 为 `00e9ce3`；最终检查时变为 `da4b5bd`（Add manifest-driven reconcile
with no hardcoded service ids），实现和验证日志已被纳入该提交，报告仍未跟踪。
本执行过程没有调用 git commit 或 git push，提交写入者未确定。没有撤销该并发提交。
因此“本执行过程未 commit/push”成立，但不能声称整个共享仓库没有发生提交。
