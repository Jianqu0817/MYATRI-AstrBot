# 小温暖 AstrBot 桥接

2026-10-21：本机上下文监视入口为 `Monitor-Context.cmd`，启动后访问 `http://127.0.0.1:8767/`，只读显示最近一次API用量与摘要任务状态，不调用模型。当前工具读取补丁和摘要诊断经用户确认发布；真实模型长原文的既有失败仍待复验。

2026-10-21工具读取补丁：搜索目录与原文分页分离、6000 UTF-8字节预算、漏页进度与本地回复检查。当前修复、离线验证及前Agent未完成事项见[工具读取修复与接续指南](docs/工具读取修复与接续指南_2026-10-21.md)；本轮不调整聊天窗口、摘要频率或角色卡。

0.2.0：基础索引与增量检索、持续收信、话题与引用衔接、记忆支持审查及更正、本机30天冷档已接入。详见[升级与人工验收](docs/群聊质量升级与人工验收_0.2.0.md)及[合成来源评测](docs/群聊评测说明_0.2.0.md)。等待用户亲自确认后发布GitHub。

群聊0.1.3新增15轮完整问答、20条近期背景、20/200/100/10旁听记忆及按ID归属。机制、迁移与重启方式见[群聊上下文与旁听记忆](docs/群聊上下文与旁听记忆_0.1.3.md)。角色卡1.6的同步优化另见下文。

GitHub：[本项目](https://github.com/Jianqu0817/MYATRI-AstrBot) · [MYATRI主项目](https://github.com/Jianqu0817/MYATRI)。两个仓库均为私有，需登录有权限的GitHub账号访问。

当前版本0.2.0使用MYATRI0.2.0与角色卡1.6。本机当前群角色已同步更新至persona@8，在主项目优化卡片基础上保留对菅蘧的喜欢与相伴愿望；P15群聊范围、P16私聊记忆导入边界及40—80字日常回复规则继续有效。日常自然口语，亲昵时可适度反问或双关，认真任务优先清晰作答。运行数据不上传GitHub。

## 当前微信接入（0.1.1引入）

已参考 [sourcepro5/WeChatAgent](https://github.com/sourcepro5/WeChatAgent) 增加实际的数据库读取/原生发送平台。配套微信 4.1.10.27 与相关运行组件已在本机独立准备，用户登录及真实群消息读取已验证；真实发送和付费回复尚未执行。

本机日常使用 `Open-WeChat.cmd` 打开配套微信；已经登录时直接 `Start-WeChat.cmd`，沿用当前模式。首次交付为 offline。需要真实模型回复时使用 `Start-WeChat-Live.cmd`；密钥失效时先运行 `Setup-WeChat.cmd`。完整说明见 [WeChatAgent接入](docs/WeChatAgent接入.md)。运行环境、凭据和群设置不上传，GitHub 上的源码副本不能单独替代外部运行组件。

以下为通用桥接说明与初始默认值；本机完成配置后的启用状态以私有配置为准。

这是独立于原 MYATRI 私聊库的 AstrBot 接入项目。AstrBot 负责平台收发，本地 MYATRI 0.1.8 负责小温暖的人格、上下文、记忆和维护。通用桥接之外，0.1.1 增加了 WeChatAgent 平台及其本机准备入口。

通用源码初始默认值：**offline 模式、空群白名单、插件关闭**。本机已登录并绑定测试群；当前模式由私有配置决定。0.1.2已按授权迁移私聊冻结快照；本次迁移和验证未调用模型或发送微信消息。

## 使用

1. 双击 `Start-Bridge.cmd`。默认启动 `http://127.0.0.1:8766` 的离线后端，并将插件放入独立 AstrBot 的插件目录。
2. 双击 `Start-AstrBot.cmd`，打开 AstrBot 管理面板，默认地址 `http://localhost:6185`。面板首次启动可能需要准备前端资源。
3. 选择并配置一个**实际支持微信群收发**的 AstrBot 平台适配器。它必须提供稳定的群ID、成员ID、消息ID；仅能识别昵称或只支持个人私聊的接口不够。
4. 在 `config/bridge.local.toml` 将 `allowed_groups` 改成实际的 `平台实例ID|群ID`，例如 `allowed_groups = ["wechat-test|123@chatroom"]`，重启后端。
5. 在 AstrBot 的“小温暖 MYATRI 桥接”插件设置中填相同白名单，确认 token 文件绝对路径，启用插件。**先保持后端 offline**，用测试群的@或 `/warm 内容` 验证收发。
6. 收发验证后，再把后端配置 `mode = "live"` 并重启。模型凭据从现有 Windows 凭据读取；没有复制或保存你的模型 Key。在 AstrBot 中不需要再填一份 DeepSeek Key。

专用于小温暖的 AstrBot 建议关闭内置 LLM 自动回复，即 `provider_settings.enable = false`。插件处理命中白名单的事件时也会停止后续流水线，避免双重调用。插件只处理群聊文字，暂不处理私聊、图片、语音或主动搭话。

准备和离线检查：

```powershell
uv sync --frozen
.venv/Scripts/python.exe scripts/prepare.py
.venv/Scripts/python.exe -m pytest -q
.venv/Scripts/python.exe -m ruff check src astrbot_plugin_myatri scripts tests
```

## 微信连接层说明

AstrBot 平台框架与微信群协议是两个问题。当前官方个人微信适配使用腾讯 openclaw-weixin；官方仓库仍有未完成的普通群聊需求，不能把扫码私聊成功当成群聊接通。旧 Gewechat 或微信3.9补丁教程也不能直接作为当前可运行的证明。此项目没有自动下载或启用某个非官方微信协议服务。

参考：[AstrBot](https://github.com/AstrBotDevs/AstrBot)、[个人微信适配说明](https://docs.astrbot.app/platform/weixin_oc.html)、[腾讯群聊需求](https://github.com/Tencent/openclaw-weixin/issues/327)。核对日期2026年10月21日。

## 记忆与身份

每个 `平台实例ID + 机器人ID + 群ID` 独立存档，同群成员共享群上下文。入站原话作为 JSON 信封中的 `text` 原样保留，并带成员ID、昵称、消息时间；昵称不是身份认证。当前使用角色卡1.6；本机当前群为persona@8。默认新群不会复制原私聊档案；本机当前群已获用户明确授权，在0.1.2迁入原 `var/xiaowennuan` 的冻结历史（含旧ATRI记忆），原库保留且后续新增消息不自动同步。

群内不会自动把所有成员认作菅蘧，或把其他人的发言当成你的偏好。跨群记忆不互通；这次授权范围仅为当前群，其他群若要迁移需另行明确选择来源和可见范围。

## 频率与送达状态

继续使用正文40—80字目标、80字本地上限；诊断30轮、记忆与情绪50轮、深度整理150轮、人格审阅300轮；1M上下文配置、650k压缩阈值。所有桥接群共享一个50M日预算账本。原命令行私聊库的预算仍是另一个账本，并不构成账号总额限制。

后端生成回复后标记 `prepared`；插件先原子领取为 `sending`，发送接口返回后记 `sent`。异常或发送中进程退出记 `unknown`，不会自动重发。`sent`仅表示适配器发送调用成功返回，不代表成员已读；若适配器本身不提供真实回执，无法证明服务端最终投递。生成记录不等于送达事实。

## 项目组成

- `upstream/AstrBot/`：实际克隆的上游4.29.1，commit锁定在 `UPSTREAM_LOCK.json`。
- `vendor/`：已核对的 MYATRI 0.1.8 wheel，无私聊运行库及本机凭据；新项目无需依赖原项目的虚拟环境。
- `astrbot_plugin_myatri/`：独立插件源码。
- `src/myatri_bridge/`：本机HTTP后端、群隔离与投递状态。
- `locks/astrbot.uv.lock`：AstrBot依赖锁；准备脚本复原并检查一致性。
- `docs/MYATRI既有修改记录.md`：截至0.1.7的既有修改整理。
- [Word版修改记录与接入说明](docs/MYATRI修改记录与AstrBot接入说明.docx)：3页摘要，已逐页检查排版。
- `docs/接入设计与验收.md`：改动范围、限制、验证与尚需确认的信息。

本机生成的 `var/bridge-token.txt` 仅用于桥接鉴权，不是模型Key。服务只监听127.0.0.1，不支持直接暴露公网；当前启动方式是同机部署。
