#!/usr/bin/env bash
set -euo pipefail

env_file="/opt/yang-ai-gateway/.env"
if [[ ! -f "$env_file" ]]; then
    echo "Missing $env_file" >&2
    exit 1
fi

read -r -s -p "请输入 DASHSCOPE_API_KEY（输入不会显示）: " api_key
echo

# Console paste may include spaces or a carriage return. Remove only leading
# and trailing whitespace; never print the resulting secret.
api_key="${api_key//$'\r'/}"
shopt -s extglob
api_key="${api_key##+([[:space:]])}"
api_key="${api_key%%+([[:space:]])}"

# Restrict the value to characters that are safe in a systemd EnvironmentFile,
# while accepting both legacy and newer DashScope key prefixes.
if [[ ! "$api_key" =~ ^[A-Za-z0-9._-]{16,256}$ ]]; then
    echo "API Key 格式仍不符合要求：请只粘贴百炼控制台中的 API Key 值，不要包含引号或标签。" >&2
    exit 1
fi

temp_file="$(mktemp /opt/yang-ai-gateway/.env.XXXXXX)"
trap 'rm -f "$temp_file"' EXIT
grep -v '^DASHSCOPE_API_KEY=' "$env_file" > "$temp_file" || true
printf 'DASHSCOPE_API_KEY=%s\n' "$api_key" >> "$temp_file"
chown --reference="$env_file" "$temp_file"
chmod 600 "$temp_file"
mv "$temp_file" "$env_file"
trap - EXIT
unset api_key
echo "DASHSCOPE_API_KEY 已安全写入 $env_file"
