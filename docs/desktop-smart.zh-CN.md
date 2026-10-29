# 自用分支：Desktop 与 Smart 自动切换

基于上游 72ccd58（0.30.0b1），保留 upstream remote 便于合并安全修复。

## Smart 策略

上游默认 best 在当前账号触线后选剩余额度最多者。consume-first 已支持优先消耗较早重置的 7d 额度。本分支新增 `smart`，复用 consume-first 的候选健康检查、二次额度刷新、过期凭据隔离、冷却和禁止立即回切逻辑：

- 当前健康时，优先消耗较早重置的周额度。
- 候选的重置时间必须至少提前 `autoswitch.resetAdvantageSeconds`（默认一小时），且位于更早的小时分组，才会离开健康的当前账号。
- 候选按周重置时间的小时分组排序，同组保持账号顺序，不随小幅用量波动洗牌。
- 当前任何窗口触线时，可以不满足重置时间优势，先保证可用；所有候选都触线时复用上游按实际剩余额度择优的兜底。
- 额度已经耗尽或账号故障可以绕过冷却；健康账号的主动优化受冷却约束。周重置时间缺失时不猜测，不为了优化主动切换。

参考 Magpie c166048 的 `internal/routes/strategies.go` 的 Smart 健康分层、按小时比较重置时间和稳定排序；没有照搬面向单次请求的策略。桌面重启成本更高，所以增加最小重置时间优势，并沿用上游防回切机制。

```sh
cswap config set autoswitch.strategy smart
cswap config set autoswitch.cooldownSeconds 600
cswap config set autoswitch.resetAdvantageSeconds 3600
cswap auto --dry-run --once
```

5h 与 7d 阈值仍可分别配置。此版本未推算账号容量或消耗速度：不同套餐的百分比不可当作相同 token 数。

## Desktop（macOS）

CLI OAuth 与桌面登录分开保存；每个桌面账号首次必须通过应用正常登录。不会将 CLI token 伪装成桌面登录。

```sh
cswap desktop launcher ccg  # 本机 zsh 已有的代理启动命令；也支持 ccgu/ccgr/default
# 在桌面版登录账号 1 后：
cswap desktop save 1
cswap desktop login        # 保存当前登录，在本机清出新登录入口；不发起服务端 logout
# 在桌面版登录账号 2 后：
cswap desktop save 2
cswap desktop list
cswap desktop switch 1     # 仅切桌面账号；首次登录失效后也可用 login/save 更新
cswap desktop sync on      # 所有托管账号均有匹配快照后才能开启
cswap desktop sync off
```

开启同步后，普通 CLI、TUI、menubar 和 auto 共用 `_perform_switch` 入口。先验证目标有匹配账号 UUID 的快照，再切 CLI，最后切桌面版；组合操作使用独立跨进程锁串行化。桌面同步出错后，CLI 已提交的结果会保留，并明确发出警告，不会冒充 CLI 失败让自动轮换重试下一个账号。

桌面正在运行时会正常退出并重新启动，**会中断当前桌面任务**。若应用未运行，只准备登录数据，下次启动生效。相同桌面账号不重复重启。`launcher ccg/ccgu/ccgr` 保留本机的代理启动方式；默认 `open -a Claude`。

快照只包含加密的桌面 OAuth 字段、Cookies 和浏览器存储；保留机器配置、MCP、工作树和原有会话索引。未复制其他账号的会话列表、云端聊天或 Cowork 任务。快照以账号 UUID 命名，账号别名和槽位移动不会串号。文件权限 600、目录权限 700；不要提交快照到 Git。恢复依赖本机 Claude Safe Storage 钥匙串，不能当作跨机器导出。

写入前保存 recovery；中途错误会恢复原登录，若进程被杀则保留 recovery 并拒绝继续覆盖，可执行 `cswap desktop recover` 恢复后核对身份。不会强杀不肯退出的桌面进程。

当前仅经过隔离的模拟文件/进程测试，**尚未在多个真实桌面账号上完成端到端验证**。本机旧桌面登录此前已清除，因此同步保持关闭，等用户完成每个账号的首次登录和 save 后再开启。

## 更新

本分支版本带 `+vin.N` 后缀。`cswap upgrade`（uv 安装）从 Vincentzyx/claude-swap 的 main 拉取，不会被 PyPI 上游版本覆盖；不再显示上游 PyPI 的更新提示。仍保留 upstream remote，后续合并上游修复后再发布本分支。
