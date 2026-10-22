# MYATRI-AstrBot HANDOFF

## HARNESS-SAME-TURN-RESULT（2026-10-22）

本轮用户不接受独立Harness报告与聊天各自回答。先读 [本轮修复](../Harness同轮结果回传修复_2026-10-22.md)，它取代下方旧报告架构说明。computer_control身份核验不再要求/pc，自然owner提供可选instructions工具；明确/pc任务和直接硬件查询才强制委派，普通聊天不自动提交。ReadingConversation在调用前保存带真实call ID的完整工具协议，再通过before_submit登记等待；ComputerTaskPending使Bridge释放锁并返回内部等待态，插件不会发送它。

computer_reporting只续接原Bridge._generate/ReadingConversation，实际feedback回填原tool消息后才继续主模型；没有单独computer_report用途请求。computer_reports仍存等待/生成退避，delivery_id改为原wx消息；trigger_kind=computer_report供现有插件轮询，不能误认为独立报告。不要恢复历史任务自动补报，也不要把queued当最终工具结果让主模型先回复。旧prepared pc_report草稿升级时superseded，sent/unknown及正文保留。状态/停止命令原直达路径保留。

用户要求快速最低验证，最终报告/续接19项通过18.84秒，Ruff/diff通过。提交前断点用例的测试信封必须与已归档原消息一致，不可为了模拟渠道附加字段只改delivery而不改raw正文，否则正确触发原消息不可变保护。早先旧中间版全量571通过/5失败，后续修复已定向覆盖，不再重跑全量或宣称全绿。生产需正常重启加载，未调用付费模型或测试群发，未绕过此前重启审批拒绝；相邻核心/角色卡/插件接口不改。Git结果以本次提交为准。

## BRIDGE-STARTUP-COLD-REPORT（2026-10-22）

用户启动失败stderr/stdout空，进程已被launcher finally清理，reader曾ready。serverimport约1.84秒，无异常。上一轮报告loop即刻tick可在aiohttp监听前调用_application冷加载，回归测试先失败（health前called已set），加await sleep1启动grace后通过，和原summaryloop保持一致。ComputerAgent队列/权限/模型账本/原任务均未改。

wechat_services新增Wait-MyatriBridgeReady（默认90秒，Stopwatch，Refresh，5秒进度，exitcode与alive超时分型，两个日志路径，不输出token/异常正文），start_wechat用-u并调用helper。server.main立即flush启动阶段python_entry/reading_config/initializing_bridge/binding_http/http_listening与PID/elapsed，不含内容。保留原清理边界和退出处理。

26项launcher/startup_delivery_repair/computer_reporting通过16.16秒，全Ruff/diff通过，--help启动标记验证。没启动生产服务、调用付费或发群测试；用户需正常重试启动器。若再失败检查stdout最后阶段，不能只找空stderr；没有宣称live完整启动通过。详见 [本轮诊断](../启动超时与空日志修复_2026-10-22.md)。

## HARNESS-DELEGATION-REPORT-LOOP（2026-10-22）

用户明确恢复原设想：主小温暖生成委派指令、Harness回报告、主模型微信汇报；并修复两段粘连。先读 [本轮说明](../任务委派与微信汇报闭环_2026-10-22.md)。旧/pc任务直入队分支已撤出，当前owner /pc任务动态schema要求instructions，主模型按原goal生成后调computer_control，合同delegation记录。固定操作与任务控制仍直达。未委派的纯文字拒绝收到一次纠正，再拒绝则ProviderError；真实错误允许汇报，不伪造提交。身份/原目标/去重仍宿主控制，子Agent提示明确goal优先。

computer_reporting.py提供持久报告outbox，Bridge.start单独任务每2秒检查终态；task+turn唯一，先原接单确认sent再生成、以原群owner源鉴权。先给报告预留上下文，主角色/原gateway tools=[] purpose=computer_report，关闭内部重试，生成3次/60s退避；事务提交prepared后不再生成。源数据和报告持久化，cancelled/unknown也如实报告。旧任务每scope仅补最新legacy，绝不重执行任务。server详情chat_report显示生成/投递/段数，claim前复核权限。已确认发送才经ack进assistant群历史，不造人类消息。失败/重启的unknown不重投。

插件initialize现在始终启动投递轮询；proactive_enabled=false时GET pending带kind=computer_report，仅汇报用户任务，不开闲聊。pending query显式可选kind与原scope校验。这次已经用prepare安装源码/配置schema到upstream的本插件目录，不改微信账号/群配置。需要正常重启整个Start-WeChat-Live以加载桥+插件；此前重启被审批拒绝，此轮不绕过。

bubbles支持无句号的单/双换行，显式分隔符短句也保留为多段，多行引语不拆；超过3段在第三段保留空行，不再无分隔拼接。真实历史只读复核，无重发。完整560项通过389.04秒，最终相关66项通过16.76秒（覆盖完整启动后追加的引语/真实委派失败反馈边界，数字重叠不相加）；Ruff/diff、wheel资产逐字节/私密排除、插件源码安装一致通过。真实Harness+离线模型的完整主委派->执行->主汇报链路、AST执行真实插件投递方法+模拟平台均验证，未手工付费/群测试。没有重启当前服务，需用户正常启动器加载，不把测试说成生产微信验收。

## HARNESS-MISSING-FINISH（2026-10-22）

用户贴5/25,190为旧11:50任务；12:07还有新任务两次pwsh成功、第三次仅文本报告，缺finish导致通用blocked。ComputerAgent._model新增completion_only内参：full纯文本先TaskStore.save_draft（WHERE running，仅改summary，不覆写pause），再仅一次递归同gateway模型收尾，tools仅FULL_FINISH；host提示真实ID、不重执行。每次仍独立正常预留/计费、调用上限、超时与不重试；收尾若发其他工具拒绝，不会进入native调度。若再纯文本则保留draft，run兜底blocked时附未完成校验说明。没有按退出码自动宣称任务完成，也没有重放生产任务。

TaskStore.report_summary仅对旧通用停止摘要，取canonical最近100事件中的实际assistant/message文本，标未验证；details与owner反馈投影复用。原summary/state/calls/tokens/events不改，已只读验证当前旧新任务可取8.90报告。新fixture实测只运行一次shell即可补finish完成；重复纯文本最多一次收尾后停车保报告；旧记录投影不改历史。最终电脑相关65项通过29.45秒，包含重点3项，不相加；Ruff/diff通过，未跑完整suite。代码需用户正常重启加载，未重试/绕过此前的自动审批拒绝。

## HARNESS-COMPLETION-AND-FEEDBACK（2026-10-22）

本轮简单修复生产任务错误call_id/空checks：ComputerAgent._model对工具反馈加host_receipt_id（原canonical journal不改），full finish失败返回completion_verification_failed、最近12实际native回执ID、纠正建议，前两次不end_turn，第三次blocked+明确校验原因；同call_id持久去重与pause/cancel优先保留。不能通过清空checks绕过验证，后台/非零退出校验不变。FULL_FINISH描述对旧恢复会话也给真实ID提示，新prompt原文/hash仅新任务使用，bounded合同不扩权。

TaskStore.feedback提供同owner+scope最近3任务/每项4个动作的CC，GroupContextBuilder在每轮owner人类同群上下文内读取。含目标、summary、回执状态/退出码和verification；排除完整命令/原stdout/文件/推理，明显key/Bearer遮蔽。仅内部system资料，提醒非指令/时间快照/不主动群外发，预算过满可删减。context.computer_tasks在Bridge._application绑定，只复制只读投影，不伪造消息/永久记忆。当前生产内存结果已经只读验证可纳入。其他成员/群不注入，不能通过昵称越权。

84项控制执行监视reset通过28.69秒、27项执行富上下文通过173.87秒（重叠）；最终新增4项复验通过3.03秒。真实Harness fixture错ID->空checks->正确ID完成且仅一个pwsh；3次错误停车、CC更新/隔离通过。Ruff/diff/JS与实机8767GET新文案通过，没有全suite/付费请求/群发送/生产任务重跑/旧状态改写。执行与CC需用户正常重启加载，下一次正常主对话才读到；监视刷新即生效。此前自动审批拒绝重启，本轮未绕过重试。不要宣称小温暖当前已经读过CC。

## HARNESS-CHAT-ADMISSION-FIX（2026-10-22）

用户反馈电脑任务被回答做不到。先读 [本轮诊断](../Harness聊天接单修复_2026-10-22.md)。三次实际请求未进入执行库：本机两处owner pin相同但少于原始渠道ID一个尾字符；带微信@前缀也不被原is_command识别。按用户明确指认的原始失败操作修正两处本地精确pin，备份/源消息证据留ignored var；禁止从昵称自动绑定或给ID做模糊匹配。

computer_control.command_body统一识别当前原话开头的/pc与@小温暖/@小溫暖+空白；固定打开/读取兼容尾@，便签与目标正文不删改。ComputerAgent.submit使用同一解析器。ReadingConversation在当轮owner handler存在且工具开启时直接dispatch，不再请主聊天LLM选择；保持raw/answer/协议/on_answer，主对话接单零API请求。回执只取真实状态及完整64位ID，元数据过滤到task_id/state/status/error；private输出留本机。原消息去重、持久claim与unknown不重放保留。不重放历史失败消息或发群测试；只读验证三个原始信封修复后均可接单。

完整543项通过370.11秒，最终电脑控制/执行59项通过24.25秒（数字重叠；后者含完整回归启动后新增的实机内存用例），真实Harness离线fixture读取物理内存并通过回执检查。Ruff与diff通过。本轮重启命令被自动审批拒绝，只有blocked by policy原因，整条命令未执行。生产尚未加载，不绕过拒绝改用其他杀进程方式。用户需正常重新运行Start-WeChat-Live.cmd；没有停止微信、AstrBot、reader、monitor，也没有自动重放旧任务。

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
