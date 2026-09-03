"""Offline smoke tests for deterministic display emotion selection."""

from app.emotion_selector import SUPPORTED_EMOTIONS, select_response_emotion


CASES = (
    ("给我讲个笑话", "当然可以。", "laughing"),
    ("我今天有点难过", "我会陪着你。", "loving"),
    ("服务器状态怎么样", "目前运行正常。", "cool"),
    ("开始一个番茄钟", "已经开始计时。", "confident"),
    ("为什么天空是蓝色", "这与光的散射有关。", "thinking"),
    ("随便聊聊", "今天很适合放松。", "relaxed"),
)


for transcript, response, expected in CASES:
    actual = select_response_emotion(transcript, response)
    assert actual == expected, (transcript, actual, expected)
    assert actual in SUPPORTED_EMOTIONS

print(f"emotion-selector-smoke-ok cases={len(CASES)}")
