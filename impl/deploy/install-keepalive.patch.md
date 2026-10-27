# 修正 `--start` 对正常退出的误报

部署者在目标机的 `install-keepalive.sh` 中，定位 `--start` 分支里
`systemctl restart "$UNIT"`、`sleep 4` 后的 `is-active` 判断，替换整个成功/失败分支。
保留原 restart 和等待逻辑。仓库没有该脚本原文，无法可靠指定数字行号；以下以语句定位，
不是已应用的远程补丁。

```bash
state=$(systemctl show --property=ActiveState --value "$UNIT") || exit 20
result=$(systemctl show --property=Result --value "$UNIT") || exit 20
code=$(systemctl show --property=ExecMainStatus --value "$UNIT") || exit 20
if [ "$state" = active ]; then
  echo "started (running)"
elif [ "$state" = inactive ] && [ "$result" = success ] && [ "$code" = 0 ]; then
  echo "started (clean exit by design, rc=0)"
else
  echo "FAILED to start: state=$state result=$result code=$code" >&2
  exit 20
fi
```

应保留 restart 本身的失败检查。此判断同时使用 ActiveState、Result、ExecMainStatus，
仅 Result=success 不足以判定成功。查询失败也退出 20。
本地 `test_m2c.py` 提取上面代码，用假 systemctl 验证运行、干净退出、异常退出、
failed/activating 状态和查询失败。部署后由部署者验证真实 unit；本任务不执行远程操作。
