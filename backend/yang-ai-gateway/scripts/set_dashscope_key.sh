#!/usr/bin/env bash
set -euo pipefail

env_file="/opt/yang-ai-gateway/.env"
if [[ ! -f "$env_file" ]]; then
    echo "Missing $env_file" >&2
    exit 1
fi

read -r -s -p "请输入 DASHSCOPE_API_KEY（输入不会显示）: " api_key
echo
if [[ ! "$api_key" =~ ^sk-[A-Za-z0-9_-]{16,}$ ]]; then
    echo "API Key 格式不正确，未修改配置。" >&2
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
