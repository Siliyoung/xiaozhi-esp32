"""Import the barge-in ASGI app with production environment settings."""

from app.conversation_barge_in import app, is_session_exit, validate_barge_in_metric
from app.conversation_streaming import email_alert_manager


assert app is not None
assert email_alert_manager.config.enabled
assert is_session_exit("你退下吧")
assert is_session_exit("你退一下吧")
assert is_session_exit("结束这次对话")
assert is_session_exit("回到待机页面")
assert not is_session_exit("别人说你退下吧是什么意思")
valid_metric = {
    "type": "client_metric", "name": "barge_in", "metric_id": 1,
    "round_trip_ms": 85, "local_clear_ms": 2, "wifi_rssi_dbm": -48,
    "free_sram_bytes": 60000, "min_free_sram_bytes": 16000,
    "uplink_frames_dropped": 0,
}
assert validate_barge_in_metric(valid_metric) is not None
assert validate_barge_in_metric({**valid_metric, "round_trip_ms": -1}) is None
assert validate_barge_in_metric({**valid_metric, "free_sram_bytes": "60000"}) is None
print("barge-in-app-import-ok email_alerts=true streaming=true session_exit=true device_metrics=true")
