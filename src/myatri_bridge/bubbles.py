"""One generation, at most three semantic chat bubbles; no extra model call."""
import re


SEPARATOR = "<|msg|>"
BUBBLE_RULES = (
    "\n发送格式：整次回复的总长度和接话焦点要求不变，不为分段增加内容。"
    "不超过15字时只写一条；超过15字时，按自然语义优先分成2条，必要时3条，"
    f"只在消息之间写专用分隔符{SEPARATOR}。每条说完整的小意思，不拆短语、引用或半句话；"
    "没有自然断点时允许一条。不写编号、JSON或发送说明，分隔符前后不得添加多余开场或收尾。"
)


def prepare_bubbles(text, policy):
    marked = SEPARATOR in text
    body = policy.prepare(text.replace(SEPARATOR, "\n\n"))
    if not body:
        return body, []
    if sum(not c.isspace() for c in body) <= 15 or "`" in body or re.search(r"https?://|www\.", body):
        return body, [body]
    if marked:
        parts = [part.strip() for part in body.split("\n\n") if part.strip()]
    else:
        # Only sentence boundaries are a fallback; never cut words to hit 15.
        boundaries = []
        for match in re.finditer(r'[。！？!?]+[”’」』）)]*', body):
            prefix = body[:match.end()]
            if any(prefix.count(left) > prefix.count(right) for left, right in
                   (("“", "”"), ("‘", "’"), ("「", "」"), ("『", "』"))) or prefix.count('"') % 2:
                continue
            boundaries.append(match.end())
        starts = [0, *boundaries]
        ends = [*boundaries, len(body)]
        parts = [body[a:b].strip() for a, b in zip(starts, ends, strict=True) if body[a:b].strip()]
    if len(parts) > 3:
        parts = [parts[0], parts[1], "".join(parts[2:])]
    return body, parts or [body]
