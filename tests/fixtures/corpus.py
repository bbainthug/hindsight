"""检索测试语料：覆盖验收语义电池的合成对话（Phase A 验收 3）。"""

from __future__ import annotations

from synthetic import T0, build_conversation, node, text_message


def retrieval_corpus_conversations() -> list[dict]:
    """构造覆盖全部匹配语义与边界情况的对话集合。

    命名锚点（与测试断言一一对应）：
    - job01 职业方向变化        → 命中「职业」「职业方向」
    - sec01 系统安全很重要      → 命中「安全」「安」（词内）
    - ai01  Learning AI daily   → 命中「AI」（含 daily 词内 ai 子串）
    - punct 你好，世界          → 命中「你好，世界」（标点连续子串）
    - hanzi 安                  → 命中单汉字「安」（回退路径）
    - seek01 求职进展顺利       → 命中「求职」
    - rel01 重复重复职业        → relevance 得分更高
    - undat 时间未知的职业话题  → 时间未知，被时间过滤排除并报告
    - mix01 用AI工程栈          → 命中「AI工程」（混合词）
    - both 职业规划与求职进展   → all_terms「职业 求职」唯一命中；literal 0
    - early 早期职业思考        → T0-12h，时区边界测试
    - wide ＡＩ工程（全角）     → snippet 原文偏移映射测试
    """
    msgs = [
        ("job01", "user", "我最近正在考虑职业方向变化", T0),
        ("sec01", "assistant", "系统安全很重要", T0 + 60),
        ("ai01", "user", "Learning AI daily", T0 + 120),
        ("punct", "user", "你好，世界", T0 + 180),
        ("hanzi", "user", "安", T0 + 240),
        ("seek01", "assistant", "求职进展顺利", T0 + 300),
        ("rel01", "user", "重复重复职业职业职业", T0 + 360),
        ("mix01", "user", "用AI工程栈搭建", T0 + 420),
        ("both", "user", "职业规划与求职进展", T0 + 480),
        ("early", "user", "早期职业思考", T0 - 43200),  # 2026-07-31T20:00Z
        ("wide", "user", "ＡＩ工程选型", T0 + 540),
    ]
    mapping: dict[str, dict] = {}
    _ = [m[0] for m in msgs]  # 顺序即链序；显式连接见下方循环
    mapping["root"] = node("root", None, None, ["job01"])[1]
    prev = "root"
    for node_id, role, text, t in msgs:
        create = t if node_id != "undat" else None
        m = text_message(node_id, role, text, create)
        mapping[node_id] = node(node_id, m, prev, [])[1]
        mapping[prev]["children"] = [node_id]
        prev = node_id

    # 无时间消息插入链尾
    undat = text_message("undat", "user", "时间未知的职业话题", None)
    mapping["undat"] = node("undat", undat, "wide", [])[1]
    mapping["wide"]["children"] = ["undat"]

    conv = build_conversation("conv-corpus", "检索语料", T0, mapping, "undat")
    # 修正 current_node 为链尾（undat 无时间也可为 current）
    conv["current_node"] = "undat"

    # 分支对话：左支不在所选路径（selected 模式排除），右支在
    b1 = text_message("br-01", "user", "分支共享祖先消息", T0 + 1000)
    b_left = text_message("br-left", "user", "左支偏理论研究", T0 + 1060)
    b_right = text_message("br-right", "user", "右支偏工程实践", T0 + 1120)
    bmap = dict(
        [
            node("root2", None, None, ["br-01"]),
            node("br-01", b1, "root2", ["br-left", "br-right"]),
            node("br-left", b_left, "br-01", []),
            node("br-right", b_right, "br-01", []),
        ]
    )
    conv_branch = build_conversation("conv-branch2", "检索分支", T0, bmap, "br-right")
    return [conv, conv_branch]
