# M2-C 实现与验收

四项本地交付已实现。M1、M2、M2-C 和 Bash 语法检查均通过，五种故意破坏均被新测试抓到。
**完整验收仍缺真实 keepalive config.json 的本地副本**：当前提供的目录、证据及本地源码归档均不包含它；
未访问远程机器。空账号测试使用明确标注的合成 JSON 和假 systemctl，不能替代真实配置验证。

所有指定验收命令、退出码、反证失败堆栈和恢复后的完整测试输出保存在
[M2C_VALIDATION.txt](M2C_VALIDATION.txt)。日志按 stdout/stderr 合并顺序保存，`$` 行为命令，`exit=` 为记录器添加的退出码。

## 四项实现的位置和关键逻辑

| 任务 | 文件:行号 | 实现 |
| --- | --- | --- |
| 1 引用解析 | `impl/bin/health.sh:27` | 所有条目的 ref 先解析，再进入原校验/探测/发布路径 |
| 2 动态目标 | `impl/bin/health.sh:55`、`impl/config/services.muse02.json:183` | A：同一 ref 上的数组空/非空映射 |
| 3 基础设施 | `impl/config/services.muse02.json:5`、`:21`、`:37` | volume/config/tunnels 放在七个服务之前，共十项 |
| 4 安装器补丁 | `impl/deploy/install-keepalive.patch.md:10` | 用运行状态或三条件干净退出替换 is-active 单条件判断 |

### 1. desired_state_ref

双来源使用字段存在性检查，`desired_state: null` 也不允许与 ref 同时存在：

```jq
(has("desired_state") | not) and
(.desired_state_ref | type == "object" and ...)
```

JSON Pointer 逐段遍历，支持空指针、空键、`~1`/`~0` 解码和标准非负数组下标。
拒绝非法转义、缺失键/下标、越界、`-`、前导零数组下标。引用文件必须是单个合法 JSON 文档。
字面值只允许 running/stopped；读文件/解析/目标错误统一调用 `fail`，退出 2。
解析后的目标仅写入内存清单，不写回业务配置或原清单。
全部引用解析完成之前不执行探测，错误时保留上次 status.json。

`status_protocol: 1` 的 stopped 成功仍显示 READY；协议 2 显示 STOPPED；整体均退出 0。

### 2. 选择 A，只有一个状态来源

```json
"desired_state_ref": {
  "file": "<KEEPALIVE_CONFIG>",
  "pointer": "/accounts",
  "when_empty": "stopped",
  "when_nonempty": "running"
}
```

A 复用文件引用和 JSON Pointer 解析路径，无须再引入独立字段及其优先级/冲突关系。
两条映射必须同时存在并取合法状态，指向的值必须为数组，禁止把 null、对象或字符串当成空账号。

```jq
if type != "array" then error("expected array")
elif length == 0 then $ref.when_empty else $ref.when_nonempty end
```

保留运行状态和日志新鲜度的合取检查。停止检查进一步要求 loaded、inactive、Result=success、
ExecMainStatus=0，防止把异常退出误算成设计终态。测试覆盖空数组 STOPPED/0、非空数组且 inactive
为 FAILED/20、非空且 active 且日志新鲜为 READY/0。

真实配置验证的下一步：由材料提供者提供真实 config.json 的本地只读副本，再将测试清单
`desired_state_ref.file` 指向它，核实 accounts=[] 并验证 STOPPED/0；不要为取得材料突破本任务的远程操作禁令。
真实 unit 的验证由后续部署者执行。

### 3. 三个基础设施前置条目

三个条目都标记 `kind: infrastructure`、`ownership: external`、`repair: null`。
本仓库没有 reconcile 实现；这些标记为其提供明确的只观察、不可自动修复契约。
后续 reconcile 必须遵守 external 所有权，不能仅凭服务数组成员身份尝试修复。

私有路径及挂载来源模式全部使用 environment 占位符：

```bash
# volume
source=$(findmnt -n -o SOURCE "$VOLUME_PATH") || exit 1
printf "%s\n" "$source" | grep -q -- "$VOLUME_SOURCE"

# config
jq -e '.version == 1' "$SITE_CONFIG" >/dev/null &&
  jq -e '.version == 1' "$SERVICES_CONFIG" >/dev/null

# tunnels 的 prerequisite；health_check 只是 exit 0
jq -e '[.relays[] | select(.reverse_port == null)] | length == 0' "$SITE_CONFIG" >/dev/null
```

tunnels 前置条件失败后复用原 WAITING 分支，跳过 health_check；单独失败时整体退出 10。
reverse_port 缺失和 null 都计为缺失。volume/config 失败仍退出 20，且 FAILED 优先于 WAITING。
无隐式全局短路：前置项也各有组件记录，后续服务照常检查，符合原逐条扫描契约。

相对旧脚本还有两点明确的收紧：findmnt 自身失败不会被下游匹配掩盖；site JSON 无效时 tunnels
不会误报 READY，而是 WAITING，同时 config 为 FAILED，整体仍是 20。

### 4. install-keepalive.sh --start

补丁文档以 restart、sleep 4 后的 is-active 分支定位；仓库无原文件，不能编造原脚本的数字行号。
保留启动操作及等待逻辑，替换为：

```bash
if [ "$state" = active ]; then
  echo "started (running)"
elif [ "$state" = inactive ] && [ "$result" = success ] && [ "$code" = 0 ]; then
  echo "started (clean exit by design, rc=0)"
else
  echo "FAILED to start: state=$state result=$result code=$code" >&2
  exit 20
fi
```

完整补丁包含三个 systemctl show 查询及查询失败 exit 20。
`impl/tests/test_m2c.py:176` 直接提取文档代码执行，覆盖 active、干净退出、非零退出、failed、
activating 和查询失败。未修改任何远程安装器。

## 硬门槛与反证

- M1 原夹具通过，未修改 `fixture_m1.py`。
- M2 通过，原有断言全部保留。`test_m2.py` 仅增加非空 accounts 临时配置、给引用接线、
  为已有停止检查补充 Result/ExecMainStatus 的假响应，以及显式字面 stopped 测试移除互斥 ref。
- 新增 `test_m2c.py` 七组测试覆盖引用、冲突、读取错误、动态目标、三个基础设施条目和安装器补丁。
- `bash -n impl/bin/health.sh` 退出 0。

`python3 impl/tests/test_m2c.py --mutations` 只破坏临时执行器/清单副本，测试断言保持原样。
每个子测试必须出现断言 FAIL 并非零退出，才能算反证被抓到；不是把语法错误当作有效反证。

| 故意破坏 | 捕获断言（实际 != 预期退出码） | 子测试退出 |
| --- | --- | --- |
| 去掉双来源互斥检查 | 0 != 2 | 1 |
| 颠倒空/非空数组目标 | 20 != 0 | 1 |
| volume 恒成功 | 0 != 20 | 1 |
| config 恒成功 | 10 != 20 | 1 |
| tunnels 从 prerequisite 改为普通 health_check | 20 != 10 | 1 |

反证结束后再运行未破坏的仓库版本：七组测试全部 OK、exit=0。原始输出在验收日志中完整保留。

## 任务描述需要更正的地方

1. accounts=0 正常退出的证据在 M2B_REPORT **第三节**；第二节是运行时依赖丢失故障。
2. 第五条验收的“应该大量 FAILED”与引用错误必须 exit 2 的要求冲突。提交清单是带
   `<KEEPALIVE_CONFIG>` 等占位符的模板；它在本机的实际结果是：

   ```text
   health: cannot resolve desired_state_ref
   exit=2
   ```

   这是正确的清单错误，不能用静默回退绕过。替换为真实可读配置后才进入服务状态探测。
3. “改判 Result=success 而非 is-active”不能按字面理解为只检查 Result；必须采用任务给出的
   ActiveState/Result/ExecMainStatus 联合判断，单独 Result=success 不能证明干净退出。
4. 所提供的本地材料不含真实 keepalive config.json，也不含 install-keepalive.sh。前者使真实配置
   验证尚未完成；后者只能给语句定位补丁，无法给原脚本的可靠数字行号。

`_evidence/`、`references/` 和 M1 夹具均未修改；未操作远程机器、未 commit、未 push。
影子清单作为既有扫描历史保留，本次部署模板为 services.muse02.json。
