#!/usr/bin/env bash
set -euo pipefail

env_file="/opt/yang-ai-gateway/.env"
service_name="yang-ai-gateway"

if [[ ! -f "$env_file" ]]; then
    echo "Configuration file not found: $env_file" >&2
    exit 1
fi

read -r -s -p "请输入高德 Web 服务 Key（输入不会显示）: " amap_key
echo
if [[ ! "$amap_key" =~ ^[A-Za-z0-9]{20,64}$ ]]; then
    echo "高德 Key 格式不正确，未修改配置。" >&2
    exit 1
fi

umask 077
candidate="$(mktemp /opt/yang-ai-gateway/.env.amap.XXXXXX)"
backup="/opt/yang-ai-gateway/.env.before-amap.$(date +%Y%m%d-%H%M%S)"
cleanup() {
    rm -f -- "$candidate"
}
trap cleanup EXIT

grep -Ev '^(AMAP_WEB_KEY|DEVICE_LOCATION_[A-Z_]+|CLOCK_WEATHER_TTL_SECONDS)=' "$env_file" > "$candidate"
printf '\nAMAP_WEB_KEY=%s\nCLOCK_WEATHER_TTL_SECONDS=1500\n' "$amap_key" >> "$candidate"
chown --reference="$env_file" "$candidate"
chmod --reference="$env_file" "$candidate"

python3 - "$candidate" <<'PY'
import json
import sys
import urllib.parse
import urllib.request

path = sys.argv[1]
key = ""
with open(path, encoding="utf-8") as config:
    for line in config:
        if line.startswith("AMAP_WEB_KEY="):
            key = line.rstrip("\n").split("=", 1)[1]
            break
query = urllib.parse.urlencode(
    {"ip": "114.247.50.2", "output": "json", "key": key}
)
with urllib.request.urlopen(
    f"https://restapi.amap.com/v3/ip?{query}", timeout=10
) as response:
    payload = json.load(response)
if payload.get("status") != "1" or payload.get("infocode") != "10000":
    raise SystemExit("高德 Key 校验失败，未修改配置。")
PY

cp --preserve=mode,ownership,timestamps "$env_file" "$backup"
mv -f -- "$candidate" "$env_file"

if ! systemctl restart "$service_name" || \
        ! systemctl is-active --quiet "$service_name"; then
    cp --preserve=mode,ownership,timestamps "$backup" "$env_file"
    systemctl restart "$service_name"
    echo "服务重启失败，配置已回滚。" >&2
    exit 1
fi

echo "高德 IP 定位已启用，固定城市配置已移除。"
