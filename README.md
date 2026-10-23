# MYATRI-AstrBot · 小温暖

通过 AstrBot 在微信群中运行小温暖：持续记忆、近期上下文、PAD 情绪、联网搜索，以及可委派的 DeepSeek Harness 电脑执行 Agent。关闭群模式后，也能在终端延续**同一群库**。

部署指南核对日期：**2026-10-23**。桥接版 **0.2.8**；当前用法以本文及代码为准，带日期的旧文档保留其历史含义。

> **本机已经配置过？** 从托盘退出其他微信 → 双击 `Open-WeChat.cmd` 并完成登录 → 双击 `Start-WeChat-Live.cmd`。只想终端聊天：退出群聊启动器后打开 `Start-Local-Chat.cmd`。不要删除 `var/` 或重新用模板覆盖配置。

## 目录

- [两个项目怎么选](#两个项目怎么选)
- [运行前准备](#运行前准备)
- [首次部署](#首次部署)
- [日常启动与模式切换](#日常启动与模式切换)
- [窗口与配置](#窗口与配置)
- [联网搜索](#联网搜索)
- [安装电脑执行 Agent](#安装电脑执行-agent)
- [记忆费用与维护](#记忆费用与维护)
- [常见问题](#常见问题)
- [更新备份与迁移](#更新备份与迁移)
- [开发与来源](#开发与来源)

## 两个项目怎么选

| 需求 | 使用入口 | 记忆位置 |
| --- | --- | --- |
| 微信群中的小温暖 | 本项目 `Start-WeChat-Live.cmd` | 本项目 `var/groups/` |
| 停止群聊，在终端延续原上下文 | 本项目 `Start-Local-Chat.cmd` | 仍为选中的原群库 |
| 不安装微信，从零开始本地聊天 | [MYATRI 内核项目](https://github.com/Jianqu0817/MYATRI) | 内核自己的 `var/xiaowennuan/` 等 |

两个仓库不会自动共享运行数据。可移植功能同步不等于复制聊天记录、账号和角色卡。本项目使用冻结的 MYATRI **0.2.8 wheel**，不要直接换成内核仓库的 0.3.0 包。

本项目的群聊与终端模式**互斥**。终端模式不是微信私聊，也不是秘密隔离库；切回群模式后，同库历史仍可参与后续上下文。

## 运行前准备

完整微信链路面向 **Windows 10/11 x64**。所需组件：

| 组件 | 要求 |
| --- | --- |
| Git | 可执行 `git --version`，能访问本仓库和 AstrBot 上游 |
| uv | 可执行 `uv --version`，负责锁定 Python 环境 |
| Python | **3.12**；项目要求 `>=3.12,<3.13`，可由 uv 安装 |
| DeepSeek | 真实聊天需要 API Key、账户额度和网络 |
| 微信接入 | WeChatAgent **0.3.9** 发行组件与配套微信 **4.1.10.27**（支持版本以 `astrbot_plugin_myatri/wechat-versions.json` 兼容表为准） |
| 电脑执行 | 可选，另需 Node.js、pnpm 与 Harness 安装 |

没有 uv 时，按 [uv 官方文档](https://docs.astral.sh/uv/getting-started/installation/) 在 PowerShell 安装：

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

安装后重新打开终端。以下命令均在本项目根目录执行；`D:\DS_workspace` 可以换成自己的位置。

**GitHub 包含**源码、提示词模板、锁文件与冻结核心 wheel。**不包含**微信发行组件、账号、读取密钥、API Key、运行库、模型文件、`.venv/`、`upstream/` 与安装后的 Harness。因此全新电脑还必须完成微信运行包准备。

## 首次部署

### 1. 克隆并准备环境

```powershell
git clone https://github.com/Jianqu0817/MYATRI-AstrBot.git
Set-Location MYATRI-AstrBot
uv python install 3.12
uv sync --frozen
.\.venv\Scripts\python.exe scripts/prepare.py
```

`prepare.py` 下载并检出锁定 AstrBot 到 `upstream/AstrBot/`，复制本插件，并在缺失时创建 `config/bridge.local.toml` 和 `var/bridge-token.txt`。已有配置、模式、渠道与令牌保留；首次默认为 `offline`。

这一步不登录微信、不调用模型，也不安装微信运行包。下载失败修复网络后重跑，不要为消除锁文件错误自行升级上游。

### 2. 保存模型密钥

```powershell
.\.venv\Scripts\python.exe -m myatri.credentials store-deepseek
```

按隐藏输入提示粘贴 Key 并回车。保存到当前 Windows 用户的凭据管理器，目标为 `MYATRI/DeepSeek/api.deepseek.com`。同一 Windows 用户下可与内核版共用；不写入 TOML，不放进命令行参数、角色卡或 GitHub。这一步不调用模型。

### 3. 准备微信运行包：新电脑不能跳过

`scripts/prepare_wechatagent_runtime.py` **只配置已有运行包，不会下载或安装**。先取得 [WeChatAgent 0.3.9 发行包](https://github.com/sourcepro5/WeChatAgent/releases/tag/v0.3.9)，按发行包说明初始化其数据工作区和匹配微信；只下载源码不够。

安装包为 `WeChatAgent-Setup-0.3.9-x64.exe`，本项目锁定 SHA256：

```text
acc17ad1927d3c65548ca781157b1751d673a8aead47af711dd4e90a5ecf06ce
```

通过 `Get-FileHash "安装包完整路径" -Algorithm SHA256` 比对。其他版本未经本项目验证，不能作为自动兼容替代。

脚本固定从以下位置找工作区：

```text
var/wechatagent-runtime/workspace/
├─ config/distribution.json
├─ config/wechatagent.json
├─ scripts/reader-api.py
├─ scripts/reader_setup.py
├─ state/hook/native/build.json
├─ state/hook/native/version.dll
├─ state/hook/native-token.txt
└─ state/hook/wechat-4.1.10.27/Weixin.exe
```

`distribution.json` 中的 Python、Node 必须实际存在。上游发行版的运行环境可能在工作区旁边的 `runtimes/`，不能删掉。

**接入已有安装工作区的方式：**先停止上游助手，选择专用于本机器人的工作区，以目录联接接入，保留原安装目录和运行环境。下面先改成自己的真实路径：

```powershell
$wechatWorkspace = "D:\WeChatAgent\data\workspace"
Test-Path "$wechatWorkspace\config\distribution.json"
New-Item -ItemType Directory -Force -Path "var\wechatagent-runtime" | Out-Null
New-Item -ItemType Junction -Path "var\wechatagent-runtime\workspace" -Target $wechatWorkspace
```

`Test-Path` 必须为 `True`，目标还需具备其余文件。已有 `workspace` 不要重复建联接或覆盖。本机已经准备过运行包则跳过此步。联接共享同一份配置，应使用**专用实例**，不要同时让上游助手处理同一账号。

也可像本机现有部署一样，将已校验组件独立放在项目 `var/`。复制数据目录时必须保留运行环境，修正 `distribution.json` 等配置中的旧绝对路径，不能只复制源码。本仓库目前没有微信冷安装的一键脚本，以上安装/联接路线未在另一台全新电脑完成实机验收。

### 4. 配置目标群与昵称

仅在没有私有配置时复制：

```powershell
Copy-Item config/wechatagent.example.toml config/wechatagent.local.toml
notepad config/wechatagent.local.toml
```

已有文件直接编辑，不覆盖。修改现有 `[wechatagent]` 表内字段：

```toml
project_dir = "D:/DS_workspace/MYATRI-AstrBot/var/wechatagent-runtime/workspace"
io_config = "state/wechat-io-config.json"
native_token = "state/hook/native-token.txt"
native_url = "http://127.0.0.1:30001"
group_name = "目标群的完整名称"
bot_nickname = "小温暖"
group_id = ""
bot_id = ""
```

路径建议使用 `/`，避免 TOML 转义错误。群必须在当前账号中可访问；群 ID 与机器人 ID 由认证检查填入，不能凭昵称猜测。随后：

```powershell
.\.venv\Scripts\python.exe scripts/prepare_wechatagent_runtime.py
```

该脚本核验 DLL 版本/哈希、安装匹配组件并关闭上游重复功能。缺少 `distribution.json` 返回第 3 步，反复 `uv sync` 无法修复。

### 5. 初始化 AstrBot，登录配套微信

1. 首次打开 `Start-AstrBot.cmd`，等待生成 AstrBot 数据文件。管理面板通常为 <http://127.0.0.1:6185/>；按实际提示设置登录信息。若缺少面板资源，按锁定 AstrBot 的启动提示准备，不能据此认定插件已经运行。
2. 初始化完成后，Ctrl+C 停止这个单独窗口，后续由总启动器管理。
3. 从托盘**完整退出其他桌面微信**；只关窗口不算退出。
4. 双击 `Open-WeChat.cmd`，打开匹配 **4.1.10.27**，自行扫码并完成手机确认。
5. 双击 `Setup-WeChat.cmd`，按提示认证读取账号。需要获取读取凭据时，按提示重新登录匹配微信。

读取密钥由当前 Windows 用户 DPAPI 加密。账号不一致、多个候选或无可认证数据库会停止，不按目录新旧猜账号。`Setup-WeChat.cmd` 配置的是**微信读取凭据**，不负责 DeepSeek Key。

### 6. 启动并绑定群

双击 `Start-WeChat-Live.cmd`。它依次检查微信接口、启动读取服务、认证账号/群、配置白名单、保存 live 模式，再启动桥接与 AstrBot。首次下载依赖可能较慢。

**这是专用 AstrBot 实例：**自动配置关闭 AstrBot 内置 LLM，并将其他平台的 `enable` 置为 `false`，避免重复回复或连接其他聊天。旧配置保存在 `var/setup-backups/`。

成功后由其他成员新发 `@小温暖 你好` 或 `/warm 你好`。机器人自己的消息不会触发。真实回复和后台维护可能产生费用；不会为了部署补发启动前的旧消息。

## 日常启动与模式切换

| 文件 | 功能 |
| --- | --- |
| `Open-WeChat.cmd` | 校验并打开配套微信，不代替本人登录 |
| `Setup-WeChat.cmd` | 首次配置或修复读取凭据 |
| `Start-WeChat-Live.cmd` | **保存 live 模式**，启动完整链路 |
| `Start-WeChat.cmd` | 启动完整链路，**沿用已保存模式** |
| `Check-WeChat.cmd` | 检查已有服务，不启动读取/发送服务 |
| `Start-Local-Chat.cmd` | 无需连接微信，在终端使用原群库 |
| `Start-Bridge.cmd` | 仅桥接后端；不要求微信在线 |
| `Start-AstrBot.cmd` | 仅 AstrBot，用于首次初始化/诊断 |

停止群模式：原总启动窗口 Ctrl+C，等读取和桥接子进程退出；配套微信保持运行。不要同时重复打开多个启动器，也不要杀掉所有 Python 或未知端口占用进程。

### 切到终端聊天

1. 停止总启动器和独立桥接实例。
2. 确认 `[bridge].owner_id` 已填写菅蘧的真实稳定发送者 ID，且不等于机器人 ID（绑定方法见电脑 Agent 小节）；再打开 `Start-Local-Chat.cmd`。单一授权群库自动选择，多库明确选择。
3. 如需指定库，运行：

```powershell
.\Start-Local-Chat.cmd --scope 这里替换为实际的32位scope
```

4. 输入文字；`/exit` 或 `/quit` 退出。未完成回合可按提示 `/retry`，复用原消息。
5. 退出终端后，按原顺序重新启动群模式。

这个入口需要**已有有效群路由和授权群库**，不会从零创建私聊库。已绑定后不要求微信、读取服务或 AstrBot 在线；live 模型仍需网络。演示可用 `Start-Local-Chat.cmd --offline`，但只是固定测试回复，不是本地大模型。

本地人类身份取配置 owner，记录标记 `transport=local_cli`；回复只显示在终端，原微信路由保留。终端和群聊共享上下文、长期记忆，访问此终端也就能使用绑定身份。

## 窗口与配置

| 地址 | 用途 | 启动方式 |
| --- | --- | --- |
| `127.0.0.1:30001` | 配套微信 native 接口 | `Open-WeChat.cmd` |
| `127.0.0.1:5031` | 默认读取服务 | 总启动器 |
| `127.0.0.1:8766` | 桥接 API，需本机 Bearer 令牌 | 总启动器/独立桥接 |
| <http://127.0.0.1:8767/> | Token、记忆/情绪、维护、回复概率 | `Monitor-Context.cmd` |
| <http://127.0.0.1:8768/> | 实际请求查看及控制 | `View-Context.cmd` |
| <http://127.0.0.1:6185/> | AstrBot 面板 | AstrBot |

两个查看器是单独入口，先启动聊天后端再打开。仅在本机回环地址使用，不要直接开放到公网。

| 文件 | 修改内容 |
| --- | --- |
| `config/bridge.local.toml` | 模式、身份、群范围、工具开关、请求采集 |
| `config/engine.toml` | 模型、上下文、情绪、维护及预算 |
| `config/wechatagent.local.toml` | 工作区、目标群、昵称、认证绑定 |
| `config/persona/persona.md` | 角色卡模板，需通过查看器重载到运行人格 |
| `config/harness/executor.local.json` | 本机 skills/MCP 扩展 |

除界面提供的热更新外，修改启动配置后正常重启。修改已有字段，不要追加重复 TOML 表；生成配置会保留旧值，升级不会把模板新值全部覆盖进去。

**动态回复概率：**打开 8767，选择群并保存回复策略。普通消息默认随机概率 `0.10`；@、叫名字和 `/warm` 另走唤醒规则。@ 或名字唤醒后关注接下来 **1—4 条人类消息**，默认跟进概率 `0.75`，10 分钟过期；不保证每条必回。界面设置持久保存，覆盖相应启动默认值。

实际请求采集默认关闭。在 `[bridge]` 中设 `capture_requests = true` 并重启，产生新请求后才有内容；采集可能包含角色卡和对话原文，不上传到公开仓库。

## 联网搜索

无需先安装电脑 Harness。修改私有桥接配置中已有表：

```toml
[bridge.web]
enabled = true
backend = "deepseek-api"
timeout_seconds = 25
max_results = 3
daily_limit = 40
cache_ttl_hours = 24
```

重启后让小温暖“查一下……”。实现直连 `https://api.deepseek.com/v1/tools/search`，复用聊天凭据，只发送检索参数。默认每群 40 次/日、缓存 24 小时；该接口必须对账户可用，权限/限额错误按真实返回处理。

搜索次数与模型 Token 是不同硬限制，费用以供应商账单为准。旧 `backend="dsh"` 仅供已配置的兼容环境使用。

## 安装电脑执行 Agent

### 1. 绑定菅蘧身份

在 `config/bridge.local.toml` 的 `[bridge]` 中修改：

```toml
owner_id = "菅蘧在当前渠道中的真实稳定账号ID"
owner_name = "菅蘧"
```

`owner_id` 是认证发送者的稳定 ID，不是昵称、群名或群 ID；不要照抄示例字符串。已有 `[bridge.computer].owner_id` 必须一致。其他成员仅改昵称为菅蘧不会获得电脑权限。

### 2. 安装锁定 Harness

准备 Node.js **22 x64，至少 22.19**，满足锁定依赖版本要求；确认 `node --version` 和 `npm --version` 可用。安装 JavaScript 版 pnpm 10，参见 [pnpm 安装说明](https://pnpm.io/installation)。这里固定旧版 JS 入口，不能把新版原生 `pnpm.exe` 或 `.cmd` 传给 `--pnpm-js`。

```powershell
npm install --prefix "var/harness-bootstrap" pnpm@10
$harnessNode = (Get-Command node.exe).Source
$harnessPnpm = (Resolve-Path "var/harness-bootstrap/node_modules/pnpm/bin/pnpm.cjs").Path
.\.venv\Scripts\python.exe scripts/setup_computer_control.py --node "$harnessNode" --pnpm-js "$harnessPnpm" --full
```

脚本按锁文件安装 DSH **0.2.1-rc.2**，复制独立 Node 到 `var/harness/`，运行无模型调用的组合探测，并仅修改电脑配置。下载依赖需要网络，不自动启动服务或发消息。

需要 skills/MCP 时，仅首次复制并编辑：

```powershell
Copy-Item config/harness/executor.example.json config/harness/executor.local.json
```

已有文件不覆盖。空配置不影响基础 PowerShell/文件工具；正常重启加载新设置。

### 3. 发任务并接收报告

由已绑定的菅蘧自然语言发起电脑请求，也可明确使用：

```text
/pc 任务 查询本机内存总量和可用内存，只读取，不修改设置
/pc 任务 检查 Python、Git、Node 版本并整理结果
/pc 状态 完整任务ID
/pc 暂停 完整任务ID
/pc 继续 完整任务ID
/pc 取消 完整任务ID
```

微信中仍需满足 @、名字或 `/warm` 的聊天触发条件。主小温暖生成执行指令，子 Agent 调用 Harness；真实状态与输出通过原 `tool_call_id` 返回**原对话回合**，小温暖收到后再生成最终回复。群模式走微信逐段发送和确认，终端模式交回终端。任务卡上的摘要/回执可供诊断。

`full` 允许在**当前 Windows 账户权限**内运行 PowerShell、文件读写/编辑/搜索、后台任务、skills 和已配置 MCP；工作目录不是权限沙箱，也不会自动获得管理员权限。桌面点击、截图和外部设备控制仍需实际可用的工具/MCP。能访问本机终端的人同样能使用绑定 owner。

任务有持久检查点、次数/时间上限，并共用模型预算；`unknown` 不自动重做可能已发生的操作。`Harness.cmd` 是独立 CLI，不替代聊天中的委派报告链路。

## 记忆费用与维护

- 近期消息、确认对话、摘要与长期记忆分层保存；身份不明或缺乏支持的候选会隔离，不能直接当事实。
- 监视页显示 PAD 情绪、心境、任务、候选及今日 Token；估价为 0 可能只是没有配置价格，不代表 API 免费。
- 默认每日 **50,000,000 Token**，桥接共用模型账本；主回复、后台维护、集成电脑 Agent 都可能消耗额度。单请求预算 1M、压缩阈值 650k、工作窗口 200k；80 组对话与 100 条近期消息是选材上限。
- 今日用量是本项目已记录用量，不是整个 DeepSeek 账户账单；另一仓库、独立 Harness CLI 和搜索不会自动全部合并。
- `[bridge.recovery].enabled = true` 默认开启有限恢复，也可在监视窗口调整；处理可恢复维护失败，保留原文与历史状态，不等于清空所有告警。
- “投递未确认”不等于没发出去，不能自动重发；候选隔离不等于原文丢失。
- 主动闲聊默认关闭，且默认影子模式；开启需核对桥接与插件两侧。**用户任务的结果报告不依赖主动闲聊开关。**
- `offline` 仅固定测试回复，不等于断网或禁止微信发送。完整群链路在 offline 下也可能发演示回复；本地模式才不发到微信。

## 常见问题

| 现象 | 处理 |
| --- | --- |
| `uv` 找不到 | 安装后重开终端，确认 `uv --version` |
| 缺少 `distribution.json` / `Weixin.exe` | 回到运行包准备步骤；`uv sync` 不会安装微信 |
| `WeChat version mismatch` | 检查实际 EXE 版本，须在兼容表（`astrbot_plugin_myatri/wechat-versions.json`）内；目录名不代表版本。新微信接入见[微信版本升级指南](docs/微信版本升级指南.md) |
| `native backend ...30001 is not running` | 托盘退出全部桌面微信，运行 `Open-WeChat.cmd` 完成登录，再开总启动器 |
| `READER_HTTP_UNAVAILABLE` | 检查器不启动读取端；开总启动器，检查 reader 日志 |
| `NATIVE_HTTP_UNAVAILABLE` | 检查配套微信、端口、版本与令牌；与模型 Key 无关 |
| 读取凭据/账号认证失败 | `Setup-WeChat.cmd`，按提示重新登录，确认读取/发送端同账号 |
| 找不到群、重名群、群被重建 | 核对群名和认证的真实群 ID，重新检查绑定，不猜 ID |
| 缺少 `cmd_config.json` | 先单独运行 AstrBot 完成初始化，再停止并开总启动器 |
| `Bridge did not start` 且 stderr 空 | 同时查看 stdout 最后启动阶段；空 stderr 不是成功依据 |
| 端口/运行库占用 | 在原窗口退出；群与终端只能开一个，不删锁强开 |
| 没回复 | 检查白名单、发送者是否为机器人、触发/冷却/概率、模式、预算和日志 |
| 固定演示回复 | 当前 offline；真实聊天用 Live 入口 |
| Key 缺失/无效 | 再执行 `myatri.credentials store-deepseek`，不是 Setup-WeChat |
| 电脑任务无权限 | 核对真实稳定 ID、owner、live 模式及电脑开关 |
| 任务受阻/无报告 | 查看任务回执、`chat_report`、桥日志；正常重启加载代码，不盲目重做 |
| 改卡没变化 | 在查看器重载到运行人格，修改文件不会自动替换 |
| 请求查看器为空 | 开启 capture、重启、产生新请求；不补采历史 |

### 微信自动升级后的恢复

检查实际版本：

```powershell
(Get-Item "var/wechatagent-runtime/workspace/state/hook/wechat-4.1.10.27/Weixin.exe").VersionInfo.FileVersion
```

本机曾出现目录仍叫 `wechat-4.1.10.27`，内部已升级为 `4.1.15.56`。先停服务、托盘退出微信、备份客户端目录，再从**已校验 0.3.9 发行组件**恢复原版 4.1.10.27，运行 `prepare_wechatagent_runtime.py`，重新打开配套微信。保留微信用户数据库和账号资料，不靠删聊天数据解决版本问题。

不要跳过版本/签名/哈希检查，或将旧 DLL 装入新版系统微信。没有匹配组件时先重新准备发行包；当前脚本不自动降级微信。

读取日志：

```powershell
Get-Content var/bridge.stdout.log -Tail 60
Get-Content var/bridge.stderr.log -Tail 60
Get-Content var/reader.stderr.log -Tail 60
```

对外提供日志前去掉对话、账号、群 ID、令牌与个人路径信息。

## 更新备份与迁移

先停聊天和查看器、备份数据，然后：

```powershell
git status --short
git pull --ff-only
uv sync --frozen
.\.venv\Scripts\python.exe scripts/prepare.py
```

有自己的已跟踪改动，先保留处理再拉取，不用 `reset --hard` 消除。更新插件后正常重启。已有配置保留，不必重绑账号；新模板设置需人工核对。

AstrBot HEAD/锁文件不符时查 [UPSTREAM_LOCK.json](UPSTREAM_LOCK.json)，不直接升级上游或更换冻结核心。

停止服务后，应备份：

1. **整个 `var/groups/`**：原始档案、上下文、SQLite、账本、任务与恢复状态，不只向量索引。
2. 私有 TOML、自己修改的 engine 配置、角色卡、Harness 本机配置及 `var/bridge-token.txt`。
3. `upstream/AstrBot/data/`、微信工作区与运行环境。目录联接要备份**实际目标**，不能只留链接。
4. 微信自身数据库另按实际位置备份，未必在仓库里。

备份放仓库外；不上传聊天、日志、数据库、令牌或 Key。换 Windows 用户/机器后，凭据管理器和 DPAPI 不能仅靠复制文件迁移，需重新保存 Key、认证微信，并修正绝对路径/目录联接。

清窗不删长期记忆。不要手改版本化记录、删 `runtime.sqlite3` 或清空任务表来去掉告警。

## 开发与来源

`src/myatri_bridge/` 为后端，`astrbot_plugin_myatri/` 为插件/微信平台，`scripts/` 为准备与启动，`config/` 为模板提示词，`vendor/` 为冻结核心包。

维护先读 [工具与接续指南](docs/工具读取修复与接续指南_2026-10-21.md)、[STATUS](docs/progress/STATUS.md) 和 [HANDOFF](docs/progress/HANDOFF.md)。相关机制：

- [命令行私聊与群聊同库](docs/命令行私聊与群聊同库_2026-10-22.md)
- [Harness 同轮结果回传](docs/Harness同轮结果回传修复_2026-10-22.md)
- [Harness 通用执行与双仓迁移](docs/Harness通用执行与双仓迁移_2026-10-22.md)
- [启动超时与空日志修复](docs/启动超时与空日志修复_2026-10-22.md)
- [WeChatAgent 接入历史记录](docs/WeChatAgent接入.md)

文档变更只需校验链接、命令入口与 diff；不必启动付费模型或微信测试发送。实现改动按范围跑定向测试。

确切来源见 [UPSTREAM_LOCK.json](UPSTREAM_LOCK.json)：[AstrBot](https://github.com/AstrBotDevs/AstrBot) 4.29.1（AGPL-3.0-or-later）、[WeChatAgent](https://github.com/sourcepro5/WeChatAgent) 0.3.9、MYATRI 0.2.8 与锁定 DeepSeek Harness npm 包。使用和分发各组件时遵守对应许可；微信等第三方二进制不随本仓库发布。
