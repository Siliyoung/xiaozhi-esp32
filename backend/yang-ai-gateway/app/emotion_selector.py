"""Fast, deterministic response-emotion selection for the device display."""

from __future__ import annotations


SUPPORTED_EMOTIONS = frozenset(
    {
        "neutral",
        "happy",
        "laughing",
        "funny",
        "sad",
        "angry",
        "crying",
        "loving",
        "embarrassed",
        "surprised",
        "shocked",
        "thinking",
        "winking",
        "cool",
        "relaxed",
        "delicious",
        "kissy",
        "confident",
        "sleepy",
        "silly",
        "confused",
    }
)


_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("loving", ("难过", "伤心", "失落", "不开心", "安慰", "抱抱", "sad", "upset")),
    ("laughing", ("笑话", "段子", "哈哈", "好笑", "搞笑", "joke", "funny", "haha")),
    ("surprised", ("竟然", "没想到", "惊喜", "不可思议", "真的吗", "居然", "wow")),
    ("sleepy", ("睡觉", "晚安", "困了", "失眠", "睡眠", "good night")),
    ("delicious", ("吃什么", "好吃", "美食", "早餐", "午餐", "晚餐", "食谱")),
    ("cool", ("服务器", "cpu", "磁盘", "网络", "代码", "编程", "程序", "部署")),
    ("confident", ("番茄钟", "完成了", "成功了", "搞定", "没问题", "可以做到")),
    ("confused", ("抱歉", "失败", "错误", "暂时不可用", "无法", "不确定", "不知道")),
    ("happy", ("你好", "谢谢", "感谢", "早上好", "下午好", "晚上好", "很高兴", "恭喜")),
    ("thinking", ("为什么", "怎么", "如何", "分析", "解释", "区别", "原因", "方案")),
    ("loving", ("喜欢你", "爱你", "想你", "love you")),
)


def select_response_emotion(transcript: str, response_text: str) -> str:
    """Choose one supported emotion without an extra model request or latency."""
    combined = f"{transcript}\n{response_text}".lower()
    for emotion, keywords in _RULES:
        if any(keyword in combined for keyword in keywords):
            return emotion
    if "!" in response_text or "！" in response_text:
        return "happy"
    return "relaxed"
