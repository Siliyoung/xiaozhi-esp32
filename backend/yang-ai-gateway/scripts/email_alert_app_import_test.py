"""Import the alert-enabled ASGI app without starting a network listener."""

from app.conversation_protected_v4 import app, email_alert_manager


assert app is not None
print(
    "email-alert-app-import-ok",
    f"enabled={str(email_alert_manager.config.enabled).lower()}",
)
