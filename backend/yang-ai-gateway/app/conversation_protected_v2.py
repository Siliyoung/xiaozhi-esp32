"""Select the leak-free persistent usage store for protected conversation."""

from app import conversation_protected as protected
from app.runtime_guard_v2 import GuardConfig, SessionLimiter, UsageStore


protected.guard_config = GuardConfig.from_environment()
protected.usage_store = UsageStore(
    protected.guard_config.usage_db_path, protected.guard_config
)
protected.session_limiter = SessionLimiter(protected.guard_config)

app = protected.app
