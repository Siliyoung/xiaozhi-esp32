"""Import the streaming ASGI app with production environment settings."""

from app.conversation_streaming import app, email_alert_manager


assert app is not None
assert email_alert_manager.config.enabled
print("streaming-app-import-ok email_alerts=true mode=streaming")
