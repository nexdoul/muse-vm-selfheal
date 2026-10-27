# cf-probe：移除安装脚本中的明文 secret

主 Agent 在部署阶段处理 `<CF_PROBE_INSTALL_SCRIPT>`。本次只交付说明，没有读取配置中的 secret、修改目标机脚本或运行真实安装器。

1. 确认 `<CF_PROBE_SECRET_FILE>`（目标部署的 `config.conf`）存在、属主为部署管理员且权限为 `600`；验证现有 unit 引用它。不要打印文件内容，也不要开启 shell xtrace。
2. 删除安装包装器中的明文 `API_SECRET` 赋值，以及向下级安装器/二进制 argv 注入 secret 的代码。只删赋值不够：旧脚本的 `ARGS` 和占位符检查也依赖该变量。
3. 后续 install 入口先检查已有 config。存在时**跳过凭据生成和覆盖**，直接复用；unit 与二进制也完整时跳过安装。unit 缺失时仍须恢复注册并启动，不能因为 config 存在就整段 `exit 0`，否则 repair 永远恢复不了丢失的 unit。
4. config 缺失时从已授权的受限文件恢复；没有凭据来源就非零退出并报告，由人工补齐。不要重新引入明文赋值、命令行 secret、日志 secret 或未经授权的在线注册。
5. 重新注册使用持久化的固定版本材料与已有 config。若所选上游安装器只接受 CLI secret，不能直接用于自动 repair；先完成配置文件接口的本地适配。不要沿用旧脚本每次下载 latest、宽泛杀进程和重装路径。

本次 `cf-probe-repair.sh` 的安装分支仅在 `LoadState=not-found` 时执行 `bash "$INSTALL_SCRIPT"`，没有 secret 参数；其部署前提是上面的安装入口已经清理。已加载而停止的 unit 走 start（遇 start-limit-hit 先 reset-failed）。active 但配置权限错误或二进制缺失时保持 FAILED，不通过重装掩盖问题。

主 Agent 部署验收应覆盖：已有 config + 缺 unit 的恢复、安装失败的非零退出、config 内容和 600 权限保持、重复执行不重装；先用假凭据和隔离服务完成验证。现有脚本含明文的清理必须先于启用新 repair，不能等到停用 watchdog 后再做。
