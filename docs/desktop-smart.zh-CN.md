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

## 原生 Desktop Code 请求账号切换（推荐，macOS）

桌面保持原来的 claude.ai 登录和工作区，模型列表与组织策略也仍由该登录决定。只在本机转发原生 Code 的 `/v1/messages`、`/v1/messages/count_tokens`，使用 `cswap` 当前活动的 OAuth 账号；不改桌面 Cookie 或桌面 token 缓存。CLI/TUI/auto 每次切换后，下一条模型请求读取新账号，已开始的响应保持原账号直到结束。无需为每次账号切换重启桌面或 Code 进程。

### 接入方式与首次启动

原生桌面会固定子进程的推理地址，并过滤用户对账号/地址的覆盖。它的 token 热更新也带账号身份校验，不能用于跨账号切换。Magpie 的桌面接入实际使用 Claude-3p 独立模式，不符合保持原桌面登录的目标。

本实现使用**显式开启的本机 HTTPS 转发**：仅对 `api.anthropic.com:443` 终止 TLS，并仅在原生 Code User-Agent 与上述模型路径同时匹配时替换凭据及 metadata 中的账号 UUID；保留设备和会话 ID。其他 API 路径、CLI、Cowork、3p 请求保留原凭据；其他域名的 CONNECT 连接保持不解密的字节隧道。未知的账号 metadata 格式直接报错，避免串号。

为此生成私有证书，并通过 `NODE_EXTRA_CA_CERTS` 仅给本次启动的桌面及其 Node 子进程信任。**不会安装系统根证书、改系统代理或修改/重签 Claude.app。** 私钥权限为 600、目录 700；本机代理随机端口，使用一次性认证口令；不记录 token、提示词、响应或请求体。原来的上游 HTTP 代理继续生效，Chromium 的 `--proxy-server` 保持原值。

首次接入需要一次正常退出和启动桌面，让新 Code 子进程继承代理与证书配置。当前正在运行的桌面不会被强制退出，启动器发现已有进程会拒绝接入。已有任务继续使用原路径；请完成它们后再自然重开。之后切换账号不需要重启。请通过接入后的 `ccg/ccgu/ccgr` 启动；直接从 Dock 启动不会接入此转发。桌面显示的账号与额度仍属于原登录，转发账号及其额度以 `cswap` 为准。

```sh
cswap desktop sync off           # 与完整桌面账号切换互斥
cswap desktop code install-launcher
cswap desktop code on
# 让自己的桌面启动函数调用 ~/.local/lib/claude-desktop/launch，保留原有代理参数。
# 本机 ccg/ccgu/ccgr 已完成此接入。
cswap desktop code status
# 完成当前工作，正常退出桌面后再运行 ccg（或 ccgu/ccgr）。
```

关闭：`cswap desktop code off`，下次正常重开后恢复原路径。切换期间不重试、不重放已发送的模型请求；上游错误原样返回。`cswap desktop code status` 提供本次进程的转发/保留原样请求计数及最后所选账号 UUID 前缀，不记录内容。活动 CLI token 到期时，复用上游的账号锁、刷新令牌消耗门闩和持久化逻辑；不另存或复制 refresh token。使用当前 CLI 登录前校验其账号/组织与托管槽位一致，无法确认时失败关闭。已有 Smart 冷却和防回切照常生效。

### 验证与边界

- 本机 Claude Desktop 2.26454.2 的已安装代码确认了原生 Code 的启动凭据/地址、身份刷新约束和受限制的测试入口；没有启用内部测试钩子。
- 真实 Claude Code 2.1.302（`claude-desktop` 入口）通过本机 TLS 代理，使用模拟上游和合成 token，在**同一个持续运行的 CLI 进程**完成两次请求，账号 A→B，两个结果都成功。
- 流式测试在 A 的响应尚未结束时切到 B，验证 A 流完整结束，下一请求用 B；验证原生 CLI/Cowork/3p/非模型路径不换凭据，其他域名保持字节隧道。
- 本机真实活动 CLI 凭据的读取与账号身份校验已通过；没有为了测试发送真实模型请求。
- 尚未在登录后的完整桌面 GUI 内做真实双账号扣额端到端验证：当前桌面进程保持运行，未为了验证重启或中断任务。后续应用升级若改变 User-Agent 或 metadata，可能需要适配；不保证第三方程序或远程 Code/VM 的流量会经过本机代理。此功能仅针对本机原生 Code。

可选运行时测试（只使用本机模拟服务和合成凭据）：

```sh
CSWAP_TEST_CLAUDE_BINARY=/path/to/claude uv run pytest -n 0 tests/test_desktop_code_runtime.py
```

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
