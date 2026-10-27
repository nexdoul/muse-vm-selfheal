# M4-B：真实分支覆盖与逐项破坏验证

## 结果

保留原有 15 个测试，新增 8 个测试，M4 共 23 项通过。23 种实现破坏逐项在临时 release 副本执行，全部产生断言 FAIL；没有把语法错误、导入错误或运行 ERROR 算作抓错。M1、M2、M2C（7 项）、M3（14 项）回归全部通过。

交付：`tests/test_m4.py`、`adapters/approver.py`、本报告及 [原始验证输出](M4B_VALIDATION.txt)。未连接或操作 muse01，未 commit/push。本次未修改 `bin/health.sh`、`bin/reconcile.sh`、`lib/reconcile-manifest.py`；这些文件中已有的工作树改动保持原状。反证对框架文件的修改仅发生在临时副本。

## 此前反证为何漏测

此前 `mutations()` 只对十个指定文本替换运行对应测试。它唯一的 identity 破坏是跳过完整 script 路径校验；`daemon(other=True)` 的确把脚本放到了不同目录，但 cwd 仍是目标 app、argv[0] 仍是 node、UID 仍相同，没有 `--check`，进程也不是僵尸。因此这个用例能抓脚本路径错误，不能证明其他身份分支正确。“十个反证被抓到”不等于所有 identity 校验已覆盖；此前报告对此表达过宽。

此前 stop 反证修改的是 health 的 desired-state marker 解析，而非 `approver.py` 的 stop 分支。框架先选 `stopped_check`，不会执行 `approver-check.sh`，所以将 `return 0 if mode == 'repair' else 1` 改成 `return 0` 能漏过。

读取本工作树时，`fresh()` 实际使用 `log.stat().st_mtime`，没有任务所述的 `grep` 或 `900000` 常量。旧测试修改 mtime，与此实现一起通过；旧反证只是删除 `healthy` 条件，不能验证日志内 ts 的契约。按本次明确要求，已把实现和测试改为日志内 ts；未把 mtime 继续当业务新鲜度证据。此前 M4A 报告关于 mtime 的说明是旧行为，以本报告为准。

## 每项身份校验如何到达

所有身份用例都创建真实 `subprocess.Popen` 子进程，读取真实 `/proc`。测试使用 Python 执行最小脚本并设置 argv[0]，契合当前实现检查 **argv basename** 的规则；不声称验证了 Node 可执行文件本身。

| 校验 | 构造与断言 |
|---|---|
| UID | 正常进程先被 identity 接受；仅 mock `os.getuid()` 为不同 UID，再断言 identity=None、main(check)=1；独立 Python 子进程使用相同 UID override 运行真实 approver 入口，退出 1。无需 root/chown。 |
| node basename | 仅 argv[0] 改成 `/usr/bin/python3`，其余与有效进程相同。 |
| cwd | 仅 cwd 改成独立 other 目录；脚本仍为目标 app 下的绝对路径。 |
| script 路径 | 同 UID、node argv、cwd，只换成 app 下 `other.cjs`。原同名异目录测试也保留。 |
| `--check` | 有效目标进程仅添加 `--check`，保持运行，避免短命进程掩盖此判断。 |
| zombie | 对真实子进程 TERM 后不 poll/wait，确认 `/proc/stat` 为 Z，再断言拒绝。Linux zombie 的 cmdline 为空且 cwd 链接不可解析，故另外仅为该 PID 提供生前 cmdline/cwd，保留真实 UID/stat，隔离验证 Z 分支。跳过 Z 判断确实使该断言失败。 |
| starttime | 校验 identity 返回真实 stat 的 starttime；在 repair 的进程发现结果中注入同 PID 的旧 token，信号前仍调用真实 identity，断言不得发 TERM。匹配 token 的正对照必须尝试 TERM。另让真实子进程退出并写入 stale pidfile，断言 identity=None、check=1。 |

node/cwd/script/`--check`、原始 zombie 和已退出 PID 均断言 identity=None、main(check)=1、shell check 退出 1，以及框架 FAILED（退出 20）。UID 的 override 跨进程显式注入；zombie 的隔离分支在同进程验证 identity 和 main(check)。

**starttime 的实际契约**：`identity(pid, root, node)` 没有“预期 starttime”参数，也不读 pidfile。它返回当前 token；repair 在发信号前比较两次观测，check 则扫描当前有效进程。因此同 PID 换成另一个身份完全符合的新实例时，check 可以为 READY，不能要求它无条件返回 None/1。写一个已退出的 PID 并不会强制内核复用 PID。本测试用真实进程配合旧 discovery token 确定性模拟复用窗口，未宣称制造了真实内核 PID 复用。发信号调用被 mock，pidfd_open 和 identity 仍真实执行。

## stop 与日志修正

`test_approver_stop` 直接检查有 stop 时 shell check 退出 1，覆盖无进程和健康进程两种状态；框架则应显示 STOPPED。把 fixture 的 `stopped_check` 改成 `exit 1`、同时把 `health_check` 改成 `exit 0` 后，必须 FAILED，证明实际选择了 stopped_check。repair 仍不启动进程、不清标记。

`fresh()` 读取 NDJSON 最后一个非空事件的数值 `ts`（Unix 毫秒），按 `MAX_STALE_SEC`（默认 900 秒）计算年龄；缺失、空日志、格式错误、非数值 ts、未来或过期均不健康。mtime 不参与判断。测试验证旧 ts + 新 mtime 失败、新 ts + 旧 mtime 成功、前条新/末条旧失败；stale repair 先断言 check=1，再验证 TERM 与后续轮次重启。该改动修复了相对本次 ts 契约的实现偏差；identity/repair 的身份规则未改变。

## 逐项破坏与实际失败输出

每项复制整个 impl，确认待替换文本恰好出现一次，改坏副本，再通过 `M4_IMPL` 指向副本运行指定用例。断言要求退出非零、输出包含 `FAIL:`，且不含 `ERROR:`。下表摘录实际 AssertionError；完整 traceback 见原始验证输出。

| 破坏点（副本） | 失败测试 | 实际失败信息 |
|---|---|---|
| identity UID bypassed (`adapters/approver.py`) | `test_identity_uid` | `AssertionError: '154679603' is not None` |
| identity node basename bypassed (`adapters/approver.py`) | `test_identity_node_basename` | `AssertionError: '154679611' is not None` |
| identity cwd bypassed (`adapters/approver.py`) | `test_identity_cwd` | `AssertionError: '154679619' is not None` |
| identity script path bypassed (`adapters/approver.py`) | `test_identity_script_path` | `AssertionError: '154679627' is not None` |
| identity --check accepted (`adapters/approver.py`) | `test_identity_check_argument` | `AssertionError: '154679635' is not None` |
| identity zombie accepted (`adapters/approver.py`) | `test_identity_zombie` | `AssertionError: '154679643' is not None` |
| identity starttime field wrong (`adapters/approver.py`) | `test_identity_starttime_reuse` | `AssertionError: '0' != '154679675'` |
| identity starttime recheck bypassed (`adapters/approver.py`) | `test_identity_starttime_reuse` | `AssertionError: Expected 'pidfd_send_signal' to not have been called. Called 1 times.` |
| stopped state uses health_check (`bin/health.sh`) | `test_approver_stop` | `AssertionError: 20 != 0`（应 STOPPED 却 FAILED） |
| stop check returns success (`adapters/approver.py`) | `test_approver_stop` | `AssertionError: 0 != 1 : ('', '')` |
| freshness threshold enlarged (`adapters/approver.py`) | `test_approver_log_timestamp` | `AssertionError: 0 != 1 : ('', '')` |
| freshness uses mtime (`adapters/approver.py`) | `test_approver_log_timestamp` | `AssertionError: 0 != 1 : ('', '')` |
| freshness reads first event (`adapters/approver.py`) | `test_approver_log_timestamp` | `AssertionError: 0 != 1 : ('', '')` |
| probe ignores permissions (`config/services.muse01.json`) | `test_cf_check` | `AssertionError: 0 != 20`（错误报 READY） |
| alias check always succeeds (`adapters/ssh-alias-check.sh`) | `test_alias_check` | `AssertionError: 0 != 20`（错误报 READY） |
| approver ignores log freshness (`adapters/approver.py`) | `test_approver_check` | `AssertionError: 0 != 20`（错误报 READY） |
| approver ignores process presence (`adapters/approver.py`) | `test_approver_check` | `AssertionError: 0 != 20`（错误报 READY） |
| stop marker ignored (`bin/health.sh`) | `test_approver_stop` | `AssertionError: 20 != 0`（应 STOPPED 却 FAILED） |
| wrong process identity accepted (`adapters/approver.py`) | `test_approver_identity` | `AssertionError: 0 != 20`（错误报 READY） |
| stale process not terminated (`adapters/approver.py`) | `test_approver_stale_repair` | `AssertionError: stale verified daemon did not receive TERM` |
| precheck bypassed (`adapters/approver.py`) | `test_approver_precheck` | `AssertionError: 0 != 20`（错误报 READY） |
| external repair guard removed (`bin/reconcile.sh`) | `test_external` | `AssertionError: True is not false` |
| loaded probe unnecessarily reinstalled (`adapters/cf-probe-repair.sh`) | `test_cf_repair_environment_and_recheck` | `AssertionError: 'start -- synthetic.service' not found`；记录出现 install |

阈值破坏在当前 Python 实现中为 `0 <= age <= maximum` → `0 <= age <= 999999.999`，对应任务中的 900000ms → 999999999ms；cwd 和 stop 的两处替换与任务完全一致。完整替换字面值保存在 `mutations()` 中。

## 验证命令与记录

```bash
python3 ../fixture_m1.py impl/bin/health.sh
python3 impl/tests/test_m2.py
python3 impl/tests/test_m2c.py
python3 impl/tests/test_m3.py
python3 impl/tests/test_m4.py
python3 impl/tests/test_m4.py --mutations
# 可只重跑某个测试对应的破坏：
python3 impl/tests/test_m4.py --mutations test_approver_stop
```

本次先完成 23 项正常测试与当时的 22 项破坏，再加强 stop 用例的 health 正对照并新增“stopped 错走 health”破坏；对最终 stop 用例重跑正常测试及其全部 3 项破坏，均符合预期。其余 20 项破坏复用未受影响的有效证据，共计 23 种独立破坏。原始记录按这两阶段保留，不把两次重复的 stop 破坏另计。所有回归退出 0。
