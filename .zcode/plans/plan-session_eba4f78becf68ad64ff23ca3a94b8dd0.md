# 微信多版本就绪 + 三仓统一迭代机制

## 已确认的事实与决策

- 上游 WeChatAgent 最新 0.4.0（10-23）仍只支持微信 **4.1.10.27**；hook DLL 在二进制层强制校验版本，**安装新微信会断链** → 按你的选择：**不装新微信**，改造成"上游哪天支持了，升级只需加表项换运行包"的多版本就绪架构。
- 长期架构按你的选择：**myatry 为主开发仓**，配自动移植脚本把改动同步到内核（`myatri/companion/` + 核心）和 AstrBot 项目（`myatri_bridge/` + 插件）。
- 现状痛点（调研结论）：版本串散落在两个项目 **8+ 处硬编码**（`SUPPORTED_VERSION` 常量、prepare/setup 脚本、`open_wechat.ps1`、UPSTREAM_LOCK、测试夹具）；`wechat_client.py`（myatry）与 `wechatagent_client.py`（AstrBot 插件）是近乎相同的两份拷贝；三仓之间无任何自动化同步。

## 第一部分：微信多版本就绪架构

### 单一数据源：版本兼容表（JSON）

新增 `wechat-versions.json`（两仓内容完全一致，同步脚本保证）：

```json
{"versions": [{
  "wechat_version": "4.1.10.27",
  "client_dir": "state/hook/wechat-4.1.10.27",
  "wechatagent_runtime": "0.3.9",
  "integration": "WeChatAgent-1",
  "backend": "wechat-hook",
  "notes": "hook DLL 二进制校验此版本；DLL 哈希以运行区 build.json 为准"
}]}
```

各配一个极简加载模块（`wechat_versions.py`，无第三方依赖），所有检查点改为查表。

### myatry（主仓）改动

1. 新增 `src/myatry/wechat_versions.py` + 内嵌 JSON 表
2. `wechat_client.py:21,171`：`SUPPORTED_VERSION` 等值比较 → 查表；不匹配时报错列出**已支持版本 + 升级指南路径**（错误码 `HOOK_VERSION_MISMATCH` 保持不变，现有行为兼容）
3. 新增 `python -m myatry versions` 只读诊断子命令：打印兼容表 + 当前运行区 build.json 实况 + 在线 hook 健康版本（可离线运行）
4. `tests/test_wechat.py`：新增"表中第二版本通过 / 表外版本拒绝且报错含支持列表"用例
5. 新增 `docs/微信版本升级指南.md`：上游发布新微信支持时的完整升级流程（下载安装包→验 SHA→装运行区→加表项→跑 check）；README 微信小节改为引用此表

### MYATRI-AstrBot 改动

1. 相同的 `wechat_versions.py` + JSON 放入 `astrbot_plugin_myatri/`；`scripts/prepare.py:57-58` 复制清单加入这两个文件（已确认是显式清单）
2. `wechatagent_client.py`：同 myatry 改表查询
3. `scripts/prepare_wechatagent_runtime.py:51,55`：字面量 → 查表（`build.json` targetVersion 必须在表内；client 目录取 `spec.client_dir`）；DLL 哈希校验维持现状（本就数据驱动）
4. `scripts/setup_wechat_reader.py:57,91`：同上查表 + EXE 路径由表推导
5. `scripts/open_wechat.ps1:4,10`：改读 `astrbot_plugin_myatri/wechat-versions.json`，自动发现 `state/hook/wechat-*` 目录并按表校验 EXE 版本；保留签名校验与 DLL 哈希校验
6. `UPSTREAM_LOCK.json`：增加 `supported_wechat_versions` 数组（记录性质）
7. `tests/test_wechatagent.py:96` 附近：补多版本用例；README 常见问题表更新

## 第二部分：三仓统一迭代机制

### 核心对齐（一次性）

内核 0.3.0 核心 vs myatry 0.2.8 核心实际只差约 17 行（`app.py` 人类消息过滤、conversation/runtime 小改）。把这 17 行**合入 myatry 的 `src/myatri/`**，使 myatry 核心成为最新超集，后续核心改动以 myatry 为源向外流。

### 自动移植脚本 `myatry/scripts/sync_repos.py`

- 模块映射表（种子来自 SOURCES.json + 内核 port 清单）：`src/myatry/<m>` ↔ AstrBot `src/myatri_bridge/<m>` ↔ 内核 `src/myatri/companion/<m>`；`src/myatri/<m>`（核心）↔ 内核 `src/myatri/<m>`
- 自动做包名改写（`myatry.` ↔ `myatri_bridge.` ↔ `myatri.companion.`）
- 输出 `docs/sync_manifest.json`（hash 清单，沿用现有文化）；目标侧有过手工适配的文件（如 `monitor.py` 清理过 AstrBot 描述）**标记 NEEDS REVIEW 而不盲写**
- 默认 dry-run，`--apply` 才落盘；核心有变动时提醒"AstrBot 项目需重建 wheel 才能吃到"（bridge 冻结 wheel 政策不变，重建是显式发布步骤）
- 顺带校验两份 `wechat-versions.json` 一致

### 流程文档

- myatry `AGENTS.md`/`docs/HANDOFF.md`：删除"旧仓库只读"，改为主仓声明 + 同步流程
- AstrBot 项目与内核的 `docs/progress/HANDOFF.md`：注明"改动由 myatry 同步流入，本地直改会被下次同步覆盖"

## 明确不做的事

- 不安装任何新版微信/WeChatAgent（0.3.9 运行区保持原样，链路零风险）
- 不修改 hook DLL、不伪造版本号绕过上游保护
- AstrBot 项目不更换冻结的 0.2.8 wheel（wheel 重建仅作为将来显式发布步骤）

## 验证

1. myatry：`pytest tests/ -q` 全过；`python -m myatry versions` 输出正确；对现有运行区跑 `bind`（只读校验）+ `wechat --offline --observe-only` 冒烟，确认 4.1.10.27 仍全链路通过
2. AstrBot 项目：`pytest tests/test_wechatagent.py` 全过；重跑 `prepare_wechatagent_runtime.py`（幂等，应无变化通过）；`open_wechat.ps1` 校验逻辑干跑
3. 同步脚本：dry-run 输出映射与 NEEDS REVIEW 清单，人工核对后对本次改动做一次真实 apply（把 `wechat_versions` 移植到 AstrBot 项目即首个实战）
4. 三个仓库分别 commit + push（沿用 conventional commits）

## 实施顺序

1. myatry：版本表 + client 改造 + versions 子命令 + 测试 + 升级指南
2. AstrBot 项目：同源移植 + 四个脚本/PS1 改造 + prepare.py 清单 + 测试
3. 核心对齐（17 行合入 myatry）
4. sync_repos.py + 三仓文档
5. 全量验证 + 三仓提交推送