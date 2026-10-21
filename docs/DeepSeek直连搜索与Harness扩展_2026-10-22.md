# DeepSeek 直连搜索与 Harness 扩展（2026-10-22）

## 范围与结论

本次主开发仓库是 `D:\DS_workspace\MYATRI-AstrBot`。用户明确：AstrBot 是主要开发版本，MYATRI CLI 在后续移植技术。本轮开始时误在相邻 MYATRI 的干净代码文件上实现了草稿，随后仅撤回本轮自己的代码/测试/公开配置修改；核心库原有 README、维护指南、STATUS/HANDOFF 改动保留。本轮最终代码仅在 AstrBot。

已读核心维护指南，梳理核心与桥接的模块、装配关系及入口，重点核对搜索、工具续接、凭据、配置、独立配额和消息投递。桥接继续依赖冻结 MYATRI 0.2.8 wheel；没有替换 vendor 或改原始档案。模块梳理不等于全仓逐行安全审计。

DeepSeek 提供独立 Search / Fetch HTTP API，使用同一 Bearer API Key。现有 `web_search` 可以由宿主直接请求 Search，省去 DSH 搜索子进程、辅助模型生成以及模型整理搜索 JSON 的步骤。主对话仍由现有 DeepSeek Chat Completions + function calling 完成，工具结果继续回到同一对话协议。

官方资料（本轮联网核对）：

- [Search / Fetch 指南](https://api-docs.deepseek.com/guides/search/)：端点、128 字限制、日期语法、响应、计费。
- [Harness 官方直连后端](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/web/web-deepseek-api/README.md)：单操作单请求，不需要辅助模型。

## 实际实现

- `src/myatri_bridge/deepseek_web.py`：固定 `POST https://api.deepseek.com/v1/tools/search`，只发送 `query/count/need_content=false`。请求时解析环境变量或 Windows 凭据，不发送 `ori_question`、角色卡、聊天上下文或文件。
- `bridge.py`：`web.backend` 默认 `deepseek-api`，显式 `dsh` 保留旧封装；未知值拒绝启动。直连后端不要求 Node/bin 路径；仍通过 `asyncio.to_thread` 执行。offline 模式拒绝直连外呼，构造不读凭据。
- `web_tools.py`：缓存、独立日限额、哈希审计、四种错误码及 6000 UTF-8 bytes 结果限制沿用。直连使用独立缓存命名空间，避免把旧辅助模型整理的缓存当成本次 API 检索；原有日次数继续累计。日限额更新改为条件 SQL 原子增加，避免并发超支。
- API `summary` 优先映射到既有 `snippet`，为空时使用 `snippet`；`title/url/snippet` 三字段结构保留，没有新增全文读取工具。外部内容始终作为不可信材料。
- 直连工具的 query 上限 111 字，为 ` after:YYYY-MM-DD` 保留 17 字；DSH 仍允许 200 字。日期按北京时间计算。后端再次检查最终不超过 128 字，不静默截掉查询或日期条件。
- 响应限制 1 MB，禁用 HTTP 重定向和环境代理；不自动重试或切换供应商。HTTP 408/504 与传输超时返回 `search_timeout`；其他 HTTP 失败返回 `search_backend_failed`；无效/过大 JSON 返回 `search_output_invalid`。不返回响应体或凭据。

本轮保留 25 秒超时、3 条默认结果、最多 5 条、24 小时缓存和每群每日 40 次**尝试**的原设置。供应商按成功且非空的请求计费，本地按尝试计数，两者有意不混称；失败和空结果仍占本地次数，缓存命中不占。搜索费用没有接入模型 50M token 账本。省去辅助模型是结构上的改变，尚未实测本次线上耗时或总费用下降。

## 配置与启用

`config/bridge.example.toml` 仍默认关闭搜索，选用 `backend="deepseek-api"`。本机 `config/bridge.local.toml` 仅新增/更改 `bridge.web.backend="deepseek-api"`；既有 `enabled=true`、live 模式、群白名单、限额、路径等保持。文件不存密钥，不提交。

正常重启 `Start-WeChat.cmd` 后新桥接进程读取代码和配置。`/reload` 只重载选定提示词，不足以切换此后端。本轮没有重启、微信发送、真实 API 调用或 GitHub 推送。DSH 回退需显式改 `backend="dsh"`，本机原有路径保留；回退只做了离线接线验证，没有重新执行真实 DSH 搜索。

`Fetch API` 确实可直接获取网页 Markdown，但原项目只有摘要搜索工具。本轮按后端替换范围没有新增 `web_fetch/web_read`，不能声称模型已读网页全文。原有 `docs/联网搜索升级.md` 是本轮开始前已存在的设计/探针记录，其中 0.88s 是历史探针，不是本轮实现实测。

## Harness 控制电脑或设备

可以。建议将 MYATRI 的人格/记忆/对话保留为上层，另设任务执行边界：

```text
已核验用户的操作请求 → MYATRI 任务工具 → 独立 Harness 执行器
  → 文件/PowerShell/浏览器/桌面 provider/MCP 或设备 API
  → 实际观察和执行回执 → MYATRI 回复与有来源的行为记录
```

官方提供[工具目录](https://deepseek-harness.github.io/deepseek-harness/en/reference/tool-catalog)、[MCP 接入](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/subsystems/mcp.md)和[实验性 computer-use provider](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/subsystems/computer-use.md)。文件/命令可走工具，桌面需合适的驱动与系统权限；看截图还要求支持图像的模型路由。智能设备、其他软件和远端服务要有对应 API/MCP/插件适配。Harness 是编排和工具执行环境，不能凭一个开关获得所有设备能力。

当前项目 `DshClient` 固定搜索用途和只读权限；现有本机版本与最新官方实验性插件的兼容性尚未验证。本轮未安装/升级 Harness 或开启电脑控制。下一步若实现，建议独立工具/profile，明确允许的程序/目录/动作、任务 ID、串行桌面执行、取消与结果核验；群成员普通发言不能自动获得本机操作权限。实际执行结果才可记作动作完成，任务意图/模型自述不算回执。不要通过放宽搜索只读 profile 来承载通用操作。

## 验证记录

定向测试：`test_deepseek_web.py`、`test_web_tools.py`、`test_web_wiring.py`，85 项通过。覆盖官方请求体和摘要映射、日期和长度、空结果、无效/过大输出、超时和 HTTP 失败、无重试/重定向、缓存隔离、并发限额、offline 拒绝真实 IO，以及搜索工具结果续接主对话；所有联网输送均为 `httpx.MockTransport`。

完整桥版 `pytest -q`：461 项通过，325.46 秒。该运行收集后补充了两个日期/工具契约边界用例，并调整直连模块的北京时间与读取期限；最终相关 85 项已重新通过。两次检查存在重叠，不把 461 与 85 相加。`ruff check src astrbot_plugin_myatri scripts tests` 及 `git diff --check` 通过。没有以旧 433 项证据替代本轮验证。
