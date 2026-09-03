"""Send one real SMTP configuration test email."""

from datetime import datetime
from zoneinfo import ZoneInfo

from app.email_alerting import EmailAlertConfig, EmailSender


def main() -> None:
    config = EmailAlertConfig.from_environment()
    if not config.enabled:
        raise RuntimeError("EMAIL_ALERT_ENABLED must be true for the SMTP test")
    now = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S")
    EmailSender(config).send(
        "[Yang AI] Email alert test succeeded",
        "The Yang AI gateway connected to the SMTP service successfully.\n\n"
        f"Test time: {now}\n"
        f"Recipient: {config.recipient}\n"
        "This test does not include conversation or device data.",
    )
    print(f"email-alert-test-ok recipient={config.recipient}")


if __name__ == "__main__":
    main()
