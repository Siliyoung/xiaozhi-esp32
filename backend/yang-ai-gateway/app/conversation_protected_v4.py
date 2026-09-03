"""Protected conversation with FastAPI-router lifecycle email alerts."""

from app import conversation_protected as protected
from app.email_alerting import EmailAlertConfig, EmailAlertManager
from app.runtime_guard_email import UsageStore
from app.runtime_guard_v2 import GuardConfig, SessionLimiter


protected.guard_config = GuardConfig.from_environment()
email_alert_config = EmailAlertConfig.from_environment()
email_alert_manager = EmailAlertManager(
    protected.guard_config.usage_db_path, email_alert_config
)
protected.usage_store = UsageStore(
    protected.guard_config.usage_db_path,
    protected.guard_config,
    email_alert_manager,
)
protected.session_limiter = SessionLimiter(protected.guard_config)

protected.app.router.add_event_handler("startup", email_alert_manager.start)
protected.app.router.add_event_handler("shutdown", email_alert_manager.stop)
app = protected.app
