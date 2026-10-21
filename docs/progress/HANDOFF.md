# MYATRI-AstrBot HANDOFF

## FULL-HARNESS-AND-DUAL-PORT（2026-10-22）

最新范围由用户明确扩展：AstrBot先完成通用执行，再将可移植功能接回MYATRI0.3.0并推两库main；只在桥版加唤醒持续关注。先读 [本轮说明](../Harness通用执行与双仓迁移_2026-10-22.md)。native-tools.mjs挂官方fs-local/pwsh-local/subprocess-local/jobs/workingDirectory/skills；工具consumer在agent scope挂载，catalog必须用schemas(scopeOf(agentCtx))，否则得到空表并错误拒绝调用。中间层tools/execute先宿主claim再next，规范结果持久化，shared provider请求留Python。MCP配置走被忽略executor.local.json，sample与lock公开；没有注入聊天Key，但full不是OS沙箱，命令享有当前用户可访问能力。

full默认100调用/2M Token/500工具/每回合1800秒，bounded旧合同12/150k/40/180保留且不自动扩权。finish_task支持receipt_success/receipt_contains；SDK后台终态是completed，非succeeded，需结合exit detail，后台启动回执不能验证完成。SDK working_directory参数是cd，不是path。真实测试覆盖PowerShell非零退出/后台收集/取消等待后阻止写入、glob/grep、stdio MCP、todo及cwd重建。read_image未提供，不能假装视觉已验证；通过full shell/MCP可按具体任务接其他能力。

attention.py只在AstrBot，表attention_windows与入站去重同事务；wake后1–4条新消息，75%与普通概率取高，跳过普通冷却，10min到期，attention类投递跟random一样过期/被新explicit淘汰。不要把该模块/配置迁入CLI或拿关注替代电脑身份核验。

桥527完整/60最终，核心749完整/100最终，数字重叠不相加。两库Ruff（核心限移植模块）、JS/wheel与core sdist通过，stage凭据模式扫描零命中。core新manifest docs/harness_port_source.json记录实际源/目标hash与排除项；不更改原port_source基线。生产仅重启核验的桥及监视，现父39840/48276，报告native-before/after/processes.json与native日志在ignored var/verification。core已独立安装并配置full，未重启私聊进程或导入群档案。GitHub同步仅用户明确授权范围，SHA以远端main为准；不提交历史untracked vendor wheel。

## HARNESS-EXECUTION-AGENT（2026-10-22）

先读 [执行Agent说明](../Harness执行Agent_2026-10-22.md)。本轮用户明确同意实施并要求写好提示词，范围仍是MYATRI-AstrBot。新增computer_agent.py/computer_tasks.py/computer_tools.py、harness/task-agent.mjs/windows-uia.ps1、prompts/executor.md；bridge接单队列/共享gateway，computer_control接零参数宿主绑定的提交/控制，server/monitor/control_proxy接本机私密API，monitor_stats新增computer_execution。没有改vendor、核心、角色卡、摘要频率或生产记忆。不要把桌面内容回传群聊；主对话只取任务ID/状态/计数，本机详情独立可读。

rc.2 SDK注意：canonical工具结果是role=tool消息的toolCallId字段，不是text块；初始seq=0标记构造时早于session/event，session/created时补取有界前缀。漏任一项会导致第二次请求协议失败或重建会话失败。常驻node进程、每任务handle.dispose，完整seed写宿主SQLite；新node进程恢复已在真实Harness离线fixture中验证。key只由Python provider解析；执行关闭thinking、retries=0，每次共享原账本，不能把node接到另一个无限额LLM。继续沿用合同及累计12次/150k Token/40工具；prompt原文及hash已持久化，不能悄悄替换旧任务授权。

效果前claim，未知结果/服务中断置unknown，不允许直接resume；人工核对后新任务。pause/cancel先记pending，等待本次有界IO结束再释放桌面锁；finish检查总UIA时间限制20秒，单driver最多15秒，取消优先于模型完成。固定pc遇锁直接busy，避免排队后意外执行。文件禁止覆盖/删除/链接/越界/设备名/ADS；UIA只允许Notepad/Calculator且invoke限计算器数字基本按钮，set_value限记事本Edit/Document，元素票据20秒及进程启动时刻绑定。后续浏览器/视觉需新工具后端与验证，当前不能宣称完整桌面控制已实测。

完整520项通过327.28秒，最终53项通过10.50秒（重叠不相加）；Ruff/JS/diff/wheel逐字节通过。只读实机UIA枚举通过，未实机点击输入，未手工调用付费模型或发测试消息。Cua浏览器被Work设置禁用，UI未做视觉验收；HTTP代理、权限与静态JS已验收。公开agent_enabled=false，本机true。已核验无foreground/在途reserved，1个running维护记录租约已过期约1700秒，未修改它；仅替换旧桥48968/49900、旧监视23564/41576。新父/子11596/30164、31992/33652，PID会变化，8768仍47996。原10%/recovery=true/revision1保存。报告var/verification/execution-agent-{install,before,after,processes}.json及新日志；源码和文档尚未提交。

## BOUNDED-RECOVERY-RUNTIME-CONTROLS（2026-10-22）

本轮用户要求诊断告警、自动处理及动态概率窗口；先读 [本轮说明](../告警诊断与动态概率_2026-10-22.md)。新增maintenance.py（核心run方法镜像，SHA及AST一致性测试；只换agent依赖）、memory_repair.py（严格逐项筛选与不可变隔离审计）、recovery.py（队列retry_failed_once原子持久版本标记）、runtime_controls.py/control_proxy.py。bridge/server接原运行时控制，monitor改为统计只读+受保护设置，不可再把整个服务描述为完全只读。

当前概率10%、recovery_enabled=true、revision1；实际浏览器保存原值验证通过，未擅自提高概率。持久设置在state_root/runtime-controls.sqlite3，后续TOML同字段不覆盖它。标记bridge-bounded-memory-v1每job一次，默认60秒巡检、安排间隔300秒，不重置attempts，不删checkpoint，不直接编辑生产任务DB，不重放unknown。切勿为清告警删除隔离或改写ID/quote。

已核验8766旧树47196→28476与8767旧树13400→45152，空闲且无received/prepared/sending后仅替换两树；现父PID48968/23564，HTTP及UI实际保存成功。微信、AstrBot、reader和8768未停。桥错误日志旧副本var/verification/bridge-before-recovery.stderr.log，运行数字报告recovery-runtime-after.json，窗口截图reply-probability-window-2026-10-22.png（均被忽略）。首次冷加载使首个读取超时，随后重读正常，不能把超时当保存失败自动重POST。

完整508项（333.47秒），最后39项（10.25秒）、Ruff/JS/diff/wheel资源核对通过；重复检查不相加。运行首个旧批次全候选不合格，本版本一次机会耗尽而停车，仍保留5failed/5deferred，需人工核对或另有明确修复后新版本策略；其余任务按退避继续。本轮未宣称生产全清。160摘要拒绝（较初查145自然增长），85unknown（22有约4小时前超时事件）为历史保留。没有手工付费验收或微信测试发送。若后续需要改缓存提炼/再生成，必须保留原审计及来源，不能改不可变记录冒充新验证。

## MONITOR-DASHBOARD（2026-10-22）

本轮用户追加UI监测需求，开发仍限本仓库。先读 [监视页扩展](../运行监视页扩展_2026-10-22.md)。代码在monitor.py、monitor_stats.py、monitor.html；context_viewer.html只补导航。今日按北京时间请求完成记账日期，API输入+输出不重复加缓存/推理；旧日志缺日期或用量不可猜。UTC共享额度另算。memory_state用只读投影适配EmotionState，不可构造会恢复/写入的ArchiveStore或MemoryIndex；投影/索引水位不是语义质量或完整性审计。

只看AstrBot群库，不再合并相邻CLI；没有迁移生产数据、换vendor/角色卡或改频率/电脑授权。完整回归498项（316.90秒），相关28+16项定向检查、Ruff/JS语法/wheel核对通过，重复检查不相加。已启动隐藏的独立监视进程（本轮PID13400，8767，仅供定位，PID会变化），输出在var/verification/monitor.stdout.log与monitor.stderr.log；微信桥接与8768没有重启。浏览器检查暂停/恢复、导出、记忆展开与告警；截图和导出样本在被忽略的var/verification。

实际只读采样显示已有历史失败/隔离、过期租约与投影水位落后；本轮只呈现状态，不表示已经修好，也不能把running账本状态当活进程。后续若用户要求修复，另行定位并用既有有限恢复流程，不从监视页自动重试或重发。

## OWNER-COMPUTER-CONTROL（2026-10-22）

本次用户授权安装升级并限制菅蘧身份。已独立安装Harness0.2.1-rc.2，未覆盖旧~/.dsh，未碰相邻核心/vendor/角色卡。先读 [升级与电脑控制](../Harness升级与电脑控制_2026-10-22.md)。配置在bridge.computer（公开关闭，本机开启且固定既有owner ID），实现computer_control.py、harness/computer-runner.mjs与ReadingConversation每轮工具factory。使用/pc三项命令，不接任意shell、桌面视觉控制。

不能把sender_name、角色卡或历史owner消息当权限。身份绑定与原话动作由宿主固定，主模型无身份/动作参数，任务claim前置且unknown不重试。实际执行经过官方Harness ToolRuntime，无二级LLM/费用。生产重启/真实消息未执行。完整回归488项（333.99秒）通过，最后Unicode边界修正后电脑控制26项复验通过，临时Harness读写、Ruff/diff和最终wheel资产核对通过，重复检查不相加。配置安装脚本只写computer表，升级入口可重跑，其他工作树修改与历史文件保留。

## DIRECT-SEARCH（2026-10-22）

主开发战场是本仓库。核心 MYATRI 本轮的误修改已仅撤回自有部分，原有文档工作树保留；勿自动修改核心或换桥版冻结0.2.8 wheel。

先读 [本轮实现](../DeepSeek直连搜索与Harness扩展_2026-10-22.md)。代码在 deepseek_web.py、bridge.py、web_tools.py；公开示例关闭联网，本机已保留开启并选 deepseek-api。启动时加载，需正常重启桥接进程；未执行真实搜索/群发送/重启。原有 docs/联网搜索升级.md 的探针速度不是本轮实测。完整回归461项通过（325.46秒），最后直连日期/期限及边界用例修改后85项定向复验通过；全目录Ruff和diff检查通过。两次检查重叠，不相加。

DSH仍只读且原封装未修改。未来电脑/设备控制需要独立任务执行器、可信用户授权、具体provider和结果回执；本轮没有激活这些能力。后续迁到CLI时只迁代码/测试/配置设计，不迁群运行库；需单独验证。
