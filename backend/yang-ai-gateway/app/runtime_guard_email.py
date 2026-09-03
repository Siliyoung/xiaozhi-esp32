"""Runtime usage store that enqueues durable quota alerts."""

from app.email_alerting import EmailAlertManager
from app.runtime_guard_v2 import UsageStore as BaseUsageStore


class UsageStore(BaseUsageStore):
    def __init__(self, path, config, alert_manager: EmailAlertManager) -> None:
        super().__init__(path, config)
        self.alert_manager = alert_manager

    def reserve_turn(self, key):
        decision = super().reserve_turn(key)
        if decision.allowed:
            day = self._day()
            self.alert_manager.enqueue_thresholds(
                day,
                "global",
                decision.total_daily_turns,
                self.config.daily_turns_total,
            )
            if self.alert_manager.config.include_device_alerts:
                self.alert_manager.enqueue_thresholds(
                    day,
                    f"device:{key}",
                    decision.device_daily_turns,
                    self.config.daily_turns_per_device,
                )
        return decision
