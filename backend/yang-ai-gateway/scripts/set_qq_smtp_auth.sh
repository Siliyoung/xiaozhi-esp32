#!/usr/bin/env bash
set -euo pipefail

env_file="/opt/yang-ai-gateway/.env"
project_dir="/opt/yang-ai-gateway"
email_address="${ALERT_EMAIL_TO:-you@example.com}"

if [[ ! -f "$env_file" ]]; then
    echo "缺少 $env_file" >&2
    exit 1
fi

read -r -s -p "请输入 QQ 邮箱 SMTP 授权码（输入不会显示）: " smtp_password
echo
smtp_password="${smtp_password//$'\r'/}"
smtp_password="${smtp_password//[[:space:]]/}"
if [[ ! "$smtp_password" =~ ^[A-Za-z0-9]{12,64}$ ]]; then
    echo "授权码格式不正确，未修改配置。请粘贴 QQ 邮箱生成的授权码，不要输入 QQ 密码。" >&2
    exit 1
fi

echo "正在通过 smtp.qq.com:465 发送测试邮件……"
if ! sudo -u ubuntu env \
    EMAIL_ALERT_ENABLED=true \
    SMTP_HOST=smtp.qq.com \
    SMTP_PORT=465 \
    SMTP_USERNAME="$email_address" \
    SMTP_PASSWORD="$smtp_password" \
    ALERT_EMAIL_FROM="$email_address" \
    ALERT_EMAIL_TO="$email_address" \
    "$project_dir/.venv/bin/python" -m scripts.email_alert_smoke_test; then
    unset smtp_password
    echo "测试邮件发送失败，未修改配置。请确认 SMTP 服务已开启且使用的是授权码。" >&2
    exit 1
fi

backup_file="$(mktemp /opt/yang-ai-gateway/.env.before-email.XXXXXX)"
temp_file="$(mktemp /opt/yang-ai-gateway/.env.XXXXXX)"
trap 'rm -f "$temp_file"' EXIT
cp -a "$env_file" "$backup_file"
grep -vE '^(EMAIL_ALERT_ENABLED|SMTP_USERNAME|SMTP_PASSWORD|ALERT_EMAIL_FROM|ALERT_EMAIL_TO)=' "$env_file" > "$temp_file" || true
printf '%s\n' \
    'EMAIL_ALERT_ENABLED=true' \
    "SMTP_USERNAME=$email_address" \
    "SMTP_PASSWORD=$smtp_password" \
    "ALERT_EMAIL_FROM=$email_address" \
    "ALERT_EMAIL_TO=$email_address" >> "$temp_file"
chown --reference="$env_file" "$temp_file"
chmod 600 "$temp_file"
mv "$temp_file" "$env_file"
trap - EXIT
unset smtp_password

if ! systemctl restart yang-ai-gateway || ! systemctl is-active --quiet yang-ai-gateway; then
    cp -a "$backup_file" "$env_file"
    systemctl restart yang-ai-gateway
    echo "服务未能使用新配置启动，已经恢复原配置。" >&2
    exit 1
fi
rm -f "$backup_file"
echo "QQ 邮箱告警已启用，测试邮件已发送到 $email_address"
