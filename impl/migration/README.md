# 本地控制器切换模板

先将新版本完整放入一个不可变 release 目录，再使用这三个入口切换。所有路径和 unit
由调用者传入；脚本依赖 Bash、Python 3.9+、systemctl，新版本还依赖 health/reconcile 的工具。

```bash
impl/migration/quiesce-old.sh --root /tmp/controller-demo --unit fake-old.service \
  --hook /tmp/demo-hooks/old-hook --hook /tmp/demo-hooks/old-bootstrap --dry-run
impl/migration/enable-new.sh --root /tmp/controller-demo --release /tmp/releases/demo \
  --services /tmp/releases/demo/config/services.json --unit fake-new.service \
  --unit-link /tmp/demo-units/fake-new.service --hook-link /tmp/demo-hooks/new-hook --dry-run
impl/migration/rollback.sh --root /tmp/controller-demo --dry-run
```

移除 `--dry-run` 才会修改本机。dry-run 不创建目录、不读取私有部署状态、不运行 systemctl；
省略实参时打印待填字段，便于任务指定的简短命令演示。真实执行必须补齐参数。

`quiesce-old` 的 `--hook` 可重复，必须列出**所有**会重新拉起旧控制器的专用入口文件，包括
bootstrap 调用入口。脚本把它们移入 root 下的停用存档，再 stop/disable 旧 unit；
事先记录 active/enabled 状态，重复相同调用不会覆盖原始快照。入口应是可独立移走的脚本或
符号链接，调度器须允许入口缺失；共享配置中的内联调用应先拆成专用入口。调用者须等待
已经开始的旧 hook/bootstrap 调用结束，再执行切换。迁移 root 必须位于调度器扫描目录外。

release 布局：

```text
bin/health.sh
bin/reconcile.sh
lib/reconcile-manifest.py
config/services.json
adapters/                 # 本版本所需的本地适配器
units/<NEW_UNIT>.service
hooks/bootstrap           # 可执行的新调度入口
```

把 `config/services.muse02.reconcile.json` 渲染为本版本的 `config/services.json`，替换占位符。
release 内的脚本、unit 和 hook 必须使用该 release 的路径，或 `ROOT/current/...`，
不得引用另一版本的代码/清单/适配器。业务目标配置与状态数据可以在 release 之外。
不再修改已打包的 release。模板不会重写 unit 文件中的部署路径。

`enable-new` 要求旧控制器已经成功 quiesce；新 unit 名须与旧名不同，新 `--unit-link`
和 `--hook-link` 路径必须空闲，父目录必须存在。`--services` 内容必须等于 release 中
`config/services.json`。预检完整布局和 schema 后，以原子替换符号链接的方式切换
`ROOT/current`；unit 与 hook 引用均通过 current。先启用 unit，再运行完整 `health.sh --json`：
退出必须为 0，status 必须 READY，组件 ID 排序后必须与清单完全相等（也会抓出重复 ID）。
还需 unit 活跃，才接通新 hook。任一步失败自动恢复旧版；预检失败也恢复已经停用的旧控制器。
已有未回滚的新部署记录时，重复 enable 会拒绝，不撤销正在运行的部署。

`rollback` 读取 root 内的事务记录，先移除新 hook、停用新 unit，再恢复 current 和 unit 引用，
最后恢复旧入口及旧 unit 的 enabled/active 状态。之前 inactive/failed 的旧 unit 保持停止，
不会为了模拟历史 failed 状态故意制造故障。旧 unit 的静态/间接安装属性由原 unit 定义保留。
回滚可重复执行；记录保留供审计，下一次 quiesce 重新取快照。三个入口共用迁移锁。

本地测试使用假 systemctl 和临时 release，覆盖成功切换、重复 quiesce/rollback、组件缺失/重复、
health 非零/非法 JSON、预检失败的恢复及 dry-run 无副作用。真实 systemd unit 的加载、
部署路径正确性、在途调度退出和两个扫描周期的运行验收仍属于目标部署阶段；本任务未执行部署。
