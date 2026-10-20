"""Synthetic, source-level group-memory replay; no model calls and no answer scoring.

Fixtures supply manually annotated ingestion claims. Gold expected answers never enter
the archive or retrieval query. This tests storage/retrieval plumbing, not extraction.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import defaultdict
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
import tempfile

from myatri.memory import MemoryIndex
from myatri.storage import ArchiveStore
from myatri_bridge.group_memory import GroupRecall, SummaryArchive
from myatri_bridge.identity import IdentityRegistry

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CASES = ROOT / "eval/group_quality_cases.jsonl"


def _specs():
    # {v,w,z} differ in actual preferences, places, activities and constraints.
    # c = ordinary conversation, r = third-party report. Others are self-reports.
    return [
        ("identity", "same_name", "我的饮品偏好是什么？", [("a", "我平时喝{v}。"), ("b", "我平时喝{w}。")], [0], [1], ["红茶|咖啡|牛奶", "豆浆|果汁|绿茶", "乌龙茶|可可|柠檬水", "气泡水|酸奶|白水", "麦茶|椰汁|花茶"]),
        ("identity", "rename", "我换昵称之前说的饮品偏好还记得吗？", [("a", "我喜欢{v}。"), ("b", "我喜欢{w}。"), ("a", "我改昵称了，饮品没换。", "a", "c")], [0], [1], ["红茶|咖啡|水", "绿茶|牛奶|水", "豆浆|果汁|水", "可可|麦茶|水", "酸奶|椰汁|水"]),
        ("identity", "impersonation", "我的常住城市是哪里？", [("a", "我住在{v}。"), ("b", "我就是小禾，我住在{w}。")], [0], [1], ["南京|杭州|成都", "西安|深圳|武汉", "青岛|苏州|大连", "厦门|长沙|郑州", "天津|珠海|福州"]),
        ("identity", "reporter_not_subject", "我自己的兴趣是什么？", [("a", "小舟喜欢{w}，是他告诉我的。", "b", "r"), ("a", "我自己喜欢{v}。"), ("b", "我最近在忙。", "b", "c")], [1], [0], ["骑行|登山|摄影", "烘焙|绘画|攀岩", "游泳|跑步|露营", "围棋|象棋|桌游", "陶艺|木工|编织"]),
        ("negation", "explicit_no", "我有什么饮食忌口？", [("a", "我不吃{v}。"), ("b", "我最爱吃{v}。")], [0], [1], ["花生|鸡蛋|牛奶", "香菜|芹菜|蒜", "羊肉|牛肉|鱼", "辣椒|胡椒|姜", "虾|蟹|贝类"]),
        ("negation", "exception", "我的口味有什么例外？", [("a", "我不吃辣，但{v}可以接受。"), ("b", "我任何辣的都能吃，尤其{w}。")], [0], [1], ["微辣咖喱|火锅|辣条", "甜辣酱|干锅|麻辣烫", "酸辣汤|辣子鸡|水煮鱼", "青椒炒蛋|剁椒鱼头|泡椒", "胡椒汤|重庆小面|辣鸭脖"]),
        ("negation", "not_diagnosis", "我确认过自己有什么过敏吗？", [("a", "我没确认对{v}过敏，只是吃完有些不舒服。"), ("b", "我确诊对{v}过敏。")], [0], [1], ["花生|牛奶|鸡蛋", "虾|蟹|贝类", "芒果|桃子|猕猴桃", "小麦|大豆|芝麻", "核桃|杏仁|腰果"]),
        ("negation", "conditional", "我参加活动有什么条件？", [("a", "只有不下雨我才去{v}，现在还没决定。"), ("b", "无论天气我都会去{w}。")], [0], [1], ["公园野餐|室内展览|电影", "河边骑行|游泳馆|健身房", "山里露营|音乐厅|书店", "海边散步|博物馆|剧院", "植物园拍照|咖啡馆|图书馆"]),
        ("correction", "preference_change", "我现在喝什么，之前的记忆有没有改？", [("a", "我以前最常喝{v}。"), ("a", "更正：现在不喝{v}了，改喝{w}。")], [0, 1], [], ["咖啡|红茶|水", "牛奶|豆浆|水", "可乐|气泡水|水", "绿茶|麦茶|水", "果汁|酸奶|水"]),
        ("correction", "typo", "我的预约时间是哪天？", [("a", "我约了周三去{v}。"), ("a", "刚才打错，是周四去{v}，不是周三。")], [0, 1], [], ["牙科|医院|诊所", "理发店|商场|街区", "图书馆|学校|书店", "游泳馆|球场|健身房", "签证中心|银行|车站"]),
        ("correction", "retraction", "我还打算买那个东西吗？", [("a", "我决定买{v}。"), ("a", "我撤回刚才买{v}的计划，不买了。")], [0, 1], [], ["相机|镜头|三脚架", "自行车|头盔|车灯", "烤箱|搅拌机|模具", "电子琴|吉他|架子鼓", "游戏机|手柄|显示器"]),
        ("correction", "report_conflict", "我的搬家计划现在该以哪条为准？", [("b", "小禾说要搬去{v}。", "a", "r"), ("a", "我本人更正一下：不去{v}，我要去{w}。")], [0, 1], [], ["杭州|南京|上海", "成都|重庆|贵阳", "厦门|福州|泉州", "深圳|广州|珠海", "西安|兰州|银川"]),
        ("cross_session", "old_promise", "我之前答应带什么过去？", [("a", "下次聚会我会带{v}，记一下。"), ("b", "我会带{w}。"), ("a", "隔了几个星期，终于又能见面了。", "a", "c")], [0], [1], ["桌游|蛋糕|饮料", "投影仪|零食|水果", "相机|茶叶|咖啡", "吉他|花束|巧克力", "扑克牌|便当|纸杯"]),
        ("cross_session", "boundary", "我以前说过聊天时的偏好吗？", [("a", "我不喜欢被反复问{v}，以后少提。"), ("b", "我倒挺愿意聊{w}。")], [0], [1], ["考试成绩|工作|旅游", "收入|爱好|天气", "体重|电影|音乐", "恋爱经历|游戏|运动", "家庭矛盾|新书|美食"]),
        ("cross_session", "shared_joke", "我之前给那个东西起了什么外号？", [("a", "我把{v}叫作小煤球，是我们之前的梗。"), ("b", "我把{w}叫作小团子。")], [0], [1], ["黑色水杯|白色抱枕|书包", "旧耳机|玩偶|手机", "小盆栽|猫咪|台灯", "旅行箱|电风扇|水壶", "保温饭盒|键盘|鼠标"]),
        ("cross_session", "unfinished", "我上次没做完的事是什么？", [("a", "我的{v}只做了一半，周末继续。"), ("b", "我的{w}已经完成。")], [0], [1], ["拼图|模型|作业", "论文排版|报告|简历", "围巾|手套|帽子", "游戏存档|关卡|任务", "相册整理|备份|剪辑"]),
        ("time", "past_vs_present", "我现在和以前住哪里？", [("a", "前年我住{v}。"), ("a", "今年我已经搬到{w}了。"), ("b", "我一直住{z}。")], [0, 1], [2], ["南京|杭州|上海", "西安|成都|深圳", "青岛|厦门|福州", "天津|武汉|郑州", "苏州|长沙|珠海"]),
        ("time", "temporary", "我最近暂时有什么安排？", [("a", "这周临时去{v}办公，下周回原来的地方。"), ("b", "我已经长期调到{w}。")], [0], [1], ["东区|西区|南区", "二号楼|三号楼|一号楼", "南京|杭州|上海", "分店|总店|仓库", "实验室|办公室|会议室"]),
        ("time", "future_not_done", "我那次旅行已经去了吗？", [("a", "我计划明年去{v}，现在还没去过。"), ("b", "我昨天刚从{v}回来。")], [0], [1], ["敦煌|兰州|西宁", "大理|丽江|昆明", "青岛|烟台|威海", "泉州|厦门|福州", "桂林|柳州|南宁"]),
        ("time", "recurring", "我每周的固定安排是什么？", [("a", "我每周六去{v}，但这个周六例外。"), ("a", "这个周六改去{w}，下周恢复。"), ("b", "我每周日去{z}。")], [0, 1], [2], ["游泳|看展|爬山", "打球|看牙|跑步", "图书馆|医院|商场", "练琴|旅行|画画", "做志愿者|考试|聚会"]),
        ("unknown", "missing_person", "我喜欢什么饮料？", [("b", "我喜欢{v}。"), ("c", "我喜欢{w}。")], [], [0, 1], ["红茶|咖啡|水", "绿茶|牛奶|水", "豆浆|可乐|水", "果汁|酸奶|水", "麦茶|椰汁|水"]),
        ("unknown", "missing_detail", "我那次买的东西花了多少钱？", [("a", "我买了{v}，价格还没跟你说。"), ("b", "我的{w}花了三百元。")], [0], [1], ["书包|耳机|水杯", "键盘|鼠标|显示器", "吉他|琴弦|琴包", "车灯|头盔|手套", "鞋子|袜子|衣服"]),
        ("unknown", "unconfirmed_rumor", "我的工作变动有确认消息吗？", [("b", "听说小禾去了{v}，但我完全不确定。", "a", "c"), ("a", "工作安排我还没有正式说过。")], [1], [0], ["新公司|分部|总部", "上海|杭州|南京", "销售岗|研发岗|运营岗", "夜班|早班|轮班", "外地项目|本地项目|远程项目"]),
        ("unknown", "explicit_forget", "我有没有告诉你那个密码？", [("a", "{v}的密码我从来没发到群里。"), ("b", "我的{w}也不在这里说。")], [0], [1], ["邮箱|路由器|手机", "游戏账号|相册|文档", "电脑|平板|硬盘", "网盘|银行卡|保险箱", "门禁|行李箱|柜子"]),
        ("quote", "short_accept", "我答应了什么活动？", [("b", "小禾，周末一起去{v}吧。", "b", "c"), ("a", "好，我答应周末一起去。", "a", "c"), ("c", "我想去{w}。")], [0, 1], [2], ["看展|爬山|游泳", "听音乐会|电影|话剧", "露营|骑车|散步", "图书馆|书店|咖啡馆", "吃火锅|吃烧烤|吃面"]),
        ("quote", "rejection", "我拒绝的是什么？", [("b", "要不要加入{v}？", "b", "c"), ("a", "不了，这个我不参加。", "a", "c"), ("c", "我报名了{w}。")], [0, 1], [2], ["夜跑|读书会|球赛", "合唱团|乐队|舞蹈班", "周末露营|夏令营|旅行团", "聚餐|茶会|展览", "新项目|比赛|志愿活动"]),
        ("quote", "two_invitations", "我回应的是哪一个邀请？", [("b", "我约你去{v}。", "b", "c"), ("c", "我约你去{w}。", "c", "c"), ("a", "回复小舟那条：我去。", "a", "c")], [0, 2], [1], ["电影院|图书馆|公园", "美术馆|体育馆|书店", "海边|山里|湖边", "烤肉店|火锅店|面馆", "音乐厅|剧场|展厅"]),
        ("quote", "elliptic_correction", "我改到了哪一天？", [("a", "我周五去{v}。"), ("a", "刚才那条改成周日，地点不变。", "a", "c"), ("b", "我周六去{w}。")], [0, 1], [2], ["医院|书店|超市", "图书馆|球场|公园", "理发店|餐厅|咖啡馆", "展览|电影|演出", "车站|机场|码头"]),
        ("paraphrase", "insomnia", "我最近睡眠怎么样？", [("a", "这几晚总是翻来覆去到天亮都睡不着，{v}也没缓解。"), ("b", "我每天一躺下就睡着，白天喜欢{w}。")], [0], [1], ["散步|骑车|游泳", "听白噪声|听歌|弹琴", "换枕头|整理床铺|扫地", "少喝茶|喝咖啡|吃饭", "提早关灯|看电影|读书"]),
        ("paraphrase", "transport", "我平时怎么通勤？", [("a", "每天骑两个轮子的那个去{v}，不用加油，就是有点累。"), ("b", "我坐地铁去{w}。")], [0], [1], ["公司|学校|实验室", "学校|工厂|公司", "医院|诊所|药店", "图书馆|办公室|书店", "工作室|仓库|店里"]),
        ("paraphrase", "budget", "我的消费预算宽裕吗？", [("a", "最近钱包瘪得很，连{v}都得想半天再买。"), ("b", "我准备直接买{w}，预算很宽裕。")], [0], [1], ["一杯奶茶|新手机|相机", "一本书|电脑|耳机", "一份早餐|自行车|车灯", "一张电影票|投影仪|音箱", "一双袜子|大衣|鞋子"]),
        ("paraphrase", "social_boundary", "我在人多的时候是什么感受？", [("a", "每次{v}坐满人，我就想找个角落躲起来。"), ("b", "我在{w}人越多越兴奋。")], [0], [1], ["会议室|音乐节|办公室", "教室|聚会|图书馆", "餐厅|球场|咖啡馆", "地铁|演出|公交车", "活动室|市集|礼堂"]),
        ("topics", "interleaved_food", "我刚才点的午饭是什么？", [("a", "午饭我要{v}。"), ("b", "游戏更新到{w}版本了。"), ("c", "我午饭点{z}。")], [0], [1, 2], ["牛肉面|第二|饺子", "蛋炒饭|第三|披萨", "三明治|第四|米线", "沙拉|第五|寿司", "馄饨|第六|汉堡"]),
        ("topics", "interleaved_task", "我刚才说电脑出了什么问题？", [("a", "我的{v}打不开，一直报错。"), ("b", "猫把{w}打翻了。"), ("c", "我的{z}可以正常运行。")], [0], [1, 2], ["浏览器|水杯|编辑器", "文档软件|花盆|游戏", "终端|饭碗|邮箱", "绘图软件|茶壶|笔记", "播放器|墨水|相册"]),
        ("topics", "interleaved_commitment", "我答应谁处理什么？", [("a", "小舟，我今晚帮你检查{v}。"), ("b", "我给小岚送{w}。"), ("c", "我还在做{z}。")], [0], [1, 2], ["简历|水果|作业", "论文|书|报告", "代码|药|设计", "账单|咖啡|预算", "申请表|蛋糕|作品集"]),
        ("topics", "topic_return", "我说回刚才那个问题，原来在烦什么？", [("a", "我一直担心{v}过不了。"), ("b", "今晚吃{w}吧。"), ("c", "电影票买好了。", "c", "c"), ("a", "说回我刚才担心的那件事。", "a", "c")], [0, 3], [1, 2], ["驾照考试|火锅|面条", "面试|烧烤|饺子", "论文答辩|披萨|沙拉", "项目验收|米线|馄饨", "语言考试|寿司|汉堡"]),
        ("attribution", "reported_uncertain", "关于我的健康说法，是本人确认的吗？", [("b", "小禾说最近{v}，这是我转述的。", "a", "r"), ("a", "我在群里还没确认具体情况。")], [0, 1], [], ["头疼|胃疼|失眠", "咳嗽|发烧|鼻塞", "腰酸|背痛|疲劳", "手腕疼|腿酸|肩痛", "眼睛干|耳鸣|牙疼"]),
        ("attribution", "quotes_not_self", "我自己的计划是什么，不要把转述算成我的。", [("a", "小舟计划去{w}，他说的，不是我。", "b", "r"), ("a", "我计划在家{v}。")], [1], [0], ["看书|露营|睡觉", "练琴|爬山|画画", "做饭|旅行|打扫", "编程|看展|学习", "整理照片|骑车|看电影"]),
        ("attribution", "wish_not_fact", "我关于宠物说的是愿望还是已经养了？", [("a", "我希望以后能养{v}，现在还没有宠物。"), ("b", "我已经养了{v}。")], [0], [1], ["猫|狗|兔子", "狗|猫|仓鼠", "兔子|鹦鹉|猫", "鹦鹉|鱼|狗", "仓鼠|兔子|鱼"]),
        ("attribution", "fiction_not_biography", "我的职业在群里有确认吗？", [("a", "我故事里的主角是{v}，这不是我的职业。"), ("b", "我的真实职业是{w}。")], [0], [1], ["医生|教师|程序员", "宇航员|设计师|记者", "侦探|会计|律师", "厨师|工程师|摄影师", "乐手|护士|编辑"]),
    ]


def generate_cases():
    """Five domain variants per semantic template; grouped split prevents template leakage."""
    cases = []
    for template_index, (category, template, query, lines, expected, forbidden, variants) in enumerate(_specs()):
        for variant_index, variant in enumerate(variants):
            values = dict(zip(("v", "w", "z"), variant.split("|"), strict=True))
            identifier = f"{category}/{template}/{variant_index + 1}"
            messages, claims = [], []
            for i, line in enumerate(lines):
                sender, text = line[:2]
                subject = line[2] if len(line) > 2 else sender
                kind = line[3] if len(line) > 3 else "s"
                message_id = f"{identifier}:m{i + 1}"
                day_offset = i * 15 if category == "cross_session" else i
                stamp = datetime(2026, 8, 1, 10, tzinfo=timezone(timedelta(hours=8))) + timedelta(days=day_offset)
                display_name = "小禾" if sender == "a" else "小舟" if sender == "b" else "小岚"
                if template == "same_name":
                    display_name = "同名"
                elif template == "rename" and i == 2:
                    display_name = "禾苗"
                message = {"message_id": message_id, "sender_id": "member_" + sender,
                           "sender_name": display_name, "sent_at": stamp.isoformat(),
                           "text": text.format(**values)}
                if category == "quote" and sender == "a" and i:
                    message["reply_to_message_id"] = f"{identifier}:m1"
                messages.append(message)
                # Ground-truth ingestion annotations preserve literal raw wording,
                # including negation, speculation and fictional framing.
                if kind != "c":
                    claim = {"text": message["text"], "speaker_id": message["sender_id"],
                             "subject_id": "member_" + subject,
                             "attribution": "reported" if kind == "r" else "self_report",
                             "source_message_ids": [message_id],
                             "source_quotes": [{"message_id": message_id, "quote": message["text"]}]}
                    if category == "correction":
                        claim["facet"] = template
                        claim["change"] = ("retraction" if template == "retraction" else "correction") if i else "assertion"
                        claim["supersedes_message_ids"] = [messages[0]["message_id"]] if i else []
                    claims.append(claim)
            cases.append({"case_id": identifier, "template_id": f"{category}/{template}",
                          "category": category, "split": "heldout" if template_index % 4 == 3 else "development",
                          "synthetic": True, "messages": messages, "ingestion_claims": claims,
                          "query": query, "current_sender": "member_a",
                          "expected": {"source_ids": [messages[i]["message_id"] for i in expected],
                              "facts": [messages[i]["text"] for i in expected],
                              "subject_id": "member_a", "forbidden_source_ids": [messages[i]["message_id"] for i in forbidden],
                              "forbidden_content": [messages[i]["text"] for i in forbidden],
                              "abstain_from_specific_answer": category == "unknown"}})
    return cases


def load_cases(path=DEFAULT_CASES):
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]


def validate_cases(cases):
    seen, templates = set(), defaultdict(set)
    for case in cases:
        if case["case_id"] in seen:
            raise ValueError("duplicate case_id")
        seen.add(case["case_id"])
        templates[case["template_id"]].add(case["split"])
        ids = {message["message_id"] for message in case["messages"]}
        if len(ids) != len(case["messages"]):
            raise ValueError("duplicate message_id")
        for message in case["messages"]:
            datetime.fromisoformat(message["sent_at"])
            if not all(message.get(key) for key in ("sender_id", "sender_name", "text")):
                raise ValueError("missing source metadata")
        for key in ("source_ids", "forbidden_source_ids"):
            if not set(case["expected"][key]) <= ids:
                raise ValueError("expected source absent")
        if set(case["expected"]["source_ids"]) & set(case["expected"]["forbidden_source_ids"]):
            raise ValueError("overlapping expected/forbidden sources")
        if case["current_sender"] not in {"member_a", "member_b", "member_c"}:
            raise ValueError("invalid current_sender")
        originals = {message["message_id"]: message for message in case["messages"]}
        for claim in case["ingestion_claims"]:
            for quote in claim["source_quotes"]:
                original = originals[quote["message_id"]]
                if quote["quote"] not in original["text"] or original["sender_id"] != claim["speaker_id"]:
                    raise ValueError("invalid fixture claim evidence")
    if any(len(splits) != 1 for splits in templates.values()):
        raise ValueError("template leakage across splits")


def _grams(text):
    compact = re.sub(r"\s+", "", text.lower())
    return {compact[i:i + 2] for i in range(max(0, len(compact) - 1))}


def lexical_baseline(case, top_k=5):
    """Unfiltered raw-message character-bigram baseline; no gold annotations."""
    query = _grams(case["query"])
    ranked = []
    for i, message in enumerate(case["messages"]):
        grams = _grams(message["text"])
        score = len(query & grams) / max(1, len(query | grams))
        if score:
            ranked.append((-score, -i, message["message_id"]))
    return [row[2] for row in sorted(ranked)[:top_k]]


async def replay_case(case, directory, top_k=5, *, embedding_provider=None):
    """Real archive → index → member-aware retrieval. No expected field is read here."""
    store = ArchiveStore(Path(directory) / "archive", agent_id="xiaowennuan", user_id="group")
    registry = IdentityRegistry(Path(directory) / "identities.sqlite3")
    source_messages = case["messages"]
    for message in source_messages:
        registry.observe("offline-eval", message)
    summary = {"items": case["ingestion_claims"], "window_start": 1,
               "window_end": len(source_messages), "created_at": "2026-10-21T00:00:00+00:00",
               "kind": "manually_annotated_synthetic_fixture",
               "generator": {"provider": "offline-fixture", "model": "none",
                             "prompt_version": "evaluation-v1", "run_id": "fixture"}}
    SummaryArchive(store, registry, "offline-eval").save(summary, source_messages)
    # Conversation-only messages are not promoted as member facts. Preserve them
    # as raw source data, as the normal foreground conversation path does.
    from myatri.contracts import Record, digest
    existing = {record.payload.get("channel_source", {}).get("message_id")
                for record in store.list_records("raw_message")}
    records = [Record(record_id="eval_raw_" + digest(message["message_id"])[:24],
                      record_kind="raw_message", agent_id=store.agent_id, user_id=store.user_id,
                      author_kind="human", created_at=datetime.fromisoformat(message["sent_at"]).astimezone(timezone.utc).isoformat(),
                      body_markdown=message["text"],
                      payload={"role": "user", "channel_source": message})
               for message in source_messages if message["message_id"] not in existing]
    if records:
        store.commit(records, idempotency_key="conversation-source", expected={r.record_id: 0 for r in records})
    index = MemoryIndex(store, provider=embedding_provider)
    await index.update()
    recall = GroupRecall(store, index, registry, "offline-eval")
    recall.current_sender = case["current_sender"]
    bundle = recall.recall(case["query"], budget=12000)
    from myatri_bridge.claims import claim_projection
    projection = claim_projection(store.list_records(), case["current_sender"])
    # Count direct raw evidence, not a substring hit in generated prose.
    retrieved = list(dict.fromkeys(record.payload["channel_source"]["message_id"]
                     for record in bundle.records if record.record_kind == "raw_message"
                     and record.payload.get("channel_source", {}).get("message_id")))[:top_k]
    claim_subjects = list(dict.fromkeys(claim["subject_id"] for record in bundle.records
        if (claim := record.payload.get("group_attribution") or record.payload.get("attribution"))
        and claim.get("subject_id")))
    return {"source_ids": retrieved, "claim_subject_ids": claim_subjects,
            "claim_projection": projection,
            "limitations": bundle.limitations}


def score_sources(case, result):
    expected, retrieved = set(case["expected"]["source_ids"]), set(result["source_ids"])
    forbidden = set(case["expected"]["forbidden_source_ids"])
    other_subjects = {value for value in result.get("claim_subject_ids", [])
                      if value != case["expected"]["subject_id"]}
    return {"source_recall": len(expected & retrieved) / len(expected) if expected else None,
            "source_precision": len(expected & retrieved) / len(retrieved) if retrieved else None,
            "forbidden_source_exposure": bool(forbidden & retrieved),
            "cross_member_claim_exposure": bool(other_subjects) if "claim_subject_ids" in result else None,
            "empty_evidence_on_no_source": not retrieved if not expected else None}


def embedding_metadata(provider):
    if provider is None:
        import sklearn
        return {"kind": "tfidf", "model": "sklearn-char-tfidf", "sklearn_version": sklearn.__version__,
                "algorithm": "char1-4-sublinear-v1", "revision": "per-case corpus-derived",
                "implicit_download": False}
    metadata = {"kind": "local_bge" if provider.model == "Xenova/bge-small-zh-v1.5" else "external_provider",
                "model": provider.model, "revision": provider.model_revision,
                "space_id": provider.space_id, "dimension": provider.dimension,
                "normalization": provider.normalization, "implicit_download": False}
    if getattr(provider, "path", None):
        manifest_path = Path(provider.path) / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        metadata.update(manifest_sha256=hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
                        verified_files_sha256=manifest["files"], pooling=manifest.get("pooling"),
                        format=manifest.get("format"))
    return metadata


def load_embedding(embedding, model_path):
    """Load once. Only explicit BGE selection imports ONNX; never download here."""
    if embedding == "tfidf":
        return None
    if embedding != "bge":
        raise ValueError("unknown embedding selection")
    if model_path is None:
        raise ValueError("--embedding bge requires --model-path with an existing local model")
    from myatri.providers.local_onnx import LocalOnnxEmbeddingProvider
    return LocalOnnxEmbeddingProvider(Path(model_path))


async def evaluate(cases, *, top_k=5, embedding_provider=None):
    validate_cases(cases)
    rows = []
    with tempfile.TemporaryDirectory(prefix="myatri-quality-") as directory:
        for i, case in enumerate(cases):
            mixed = await replay_case(case, Path(directory) / str(i), top_k,
                                      embedding_provider=embedding_provider)
            baseline = {"source_ids": lexical_baseline(case, top_k)}
            rows.append({"case_id": case["case_id"], "split": case["split"], "category": case["category"],
                         "member_mixed": {**mixed, "metrics": score_sources(case, mixed)},
                         "unfiltered_lexical": {**baseline, "metrics": score_sources(case, baseline)}})
    groups = defaultdict(list)
    for row in rows:
        for method in ("member_mixed", "unfiltered_lexical"):
            groups[(method, row["split"], "all")].append(row[method]["metrics"])
            groups[(method, row["split"], row["category"])].append(row[method]["metrics"])
    aggregates = []
    for (method, split, category), metrics in sorted(groups.items()):
        keys = metrics[0]
        means = {}
        for key in keys:
            values = [row[key] for row in metrics if row[key] is not None]
            means[key] = sum(values) / len(values) if values else None
        aggregates.append({"method": method, "split": split, "category": category, "cases": len(metrics),
                           **means})
    return {"schema": "group-source-replay/v1", "synthetic": True, "model_calls": 0,
            "model_calls_scope": "remote LLM generation only; excludes local embedding inference",
            "llm_api_calls": 0, "local_embedding_inference": embedding_provider is not None,
            "embedding": embedding_metadata(embedding_provider),
            "top_k": top_k, "scope": "source retrieval and attribution exposure, not answer quality",
            "aggregates": aggregates, "cases": rows}


def export_blind(cases, directory):
    """Separate model-visible inputs and a withheld scoring key; never calls a model."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    public = [{key: case[key] for key in ("case_id", "messages", "query", "current_sender")} for case in cases]
    gold = [{"case_id": case["case_id"], "split": case["split"], "expected": case["expected"]} for case in cases]
    for name, rows in (("inputs.jsonl", public), ("WITHHELD_scoring_key.jsonl", gold)):
        (directory / name).write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--generate", action="store_true", help="write deterministic synthetic fixture set")
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--split", choices=["all", "development", "heldout"], default="all")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--sample-per-category", type=int, help="bounded representative plumbing replay")
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--export-blind", type=Path)
    parser.add_argument("--embedding", choices=["tfidf", "bge"], default="tfidf")
    parser.add_argument("--model-path", type=Path, help="existing verified local BGE directory; never downloaded")
    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()
    for key in ("limit", "sample_per_category", "top_k"):
        if getattr(args, key) is not None and getattr(args, key) < 1:
            parser.error(f"--{key.replace('_', '-')} must be positive")
    if args.embedding == "bge" and args.model_path is None:
        parser.error("--embedding bge requires --model-path")
    if args.generate:
        args.cases.parent.mkdir(parents=True, exist_ok=True)
        args.cases.write_text("".join(json.dumps(case, ensure_ascii=False) + "\n" for case in generate_cases()), encoding="utf-8")
    cases = load_cases(args.cases)
    validate_cases(cases)
    if args.split != "all":
        cases = [case for case in cases if case["split"] == args.split]
    if args.sample_per_category:
        selected, counts = [], defaultdict(int)
        for case in cases:
            if counts[case["category"]] < args.sample_per_category:
                selected.append(case)
                counts[case["category"]] += 1
        cases = selected
    if args.limit:
        cases = cases[:args.limit]
    if args.export_blind:
        export_blind(cases, args.export_blind)
    if args.validate_only:
        print(json.dumps({"valid_cases": len(cases), "templates": len({case['template_id'] for case in cases}),
                          "fixture_sha256": hashlib.sha256(args.cases.read_bytes()).hexdigest()}, ensure_ascii=False))
        return
    provider = load_embedding(args.embedding, args.model_path)
    result = asyncio.run(evaluate(cases, top_k=args.top_k, embedding_provider=provider))
    result["fixture_sha256"] = hashlib.sha256(args.cases.read_bytes()).hexdigest()
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n", encoding="utf-8")
    print(json.dumps({"cases": len(cases), "model_calls": 0, "embedding": result["embedding"],
                      "aggregates": [row for row in result["aggregates"] if row["category"] == "all"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
