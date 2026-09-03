# Yang AI Gateway

第一阶段协议网关：向 ESP32 下发自有 WebSocket 地址，完成鉴权、hello 握手，接收设备上传的 Opus 帧。当前版本不调用 ASR、LLM 或 TTS。

## 本地检查

```powershell
cd backend/yang-ai-gateway
Copy-Item .env.example .env
```

将 `.env` 中的 `DEVICE_TOKEN` 替换为至少 32 位的随机字符串，再执行：

```powershell
docker compose config
docker compose up -d --build
curl.exe http://127.0.0.1:18000/health
curl.exe -X POST http://127.0.0.1:18000/robot/ota/
```

## 上传服务器

项目建议部署到 `/opt/yang-ai-gateway`。SSH 公钥配置完成后，在本机项目根目录执行：

```powershell
scp -r backend/yang-ai-gateway ubuntu@YOUR_SERVER_IP:/tmp/
ssh ubuntu@YOUR_SERVER_IP "sudo mkdir -p /opt/yang-ai-gateway && sudo cp -a /tmp/yang-ai-gateway/. /opt/yang-ai-gateway/ && sudo chown -R ubuntu:ubuntu /opt/yang-ai-gateway"
```

登录服务器后：

```bash
cd /opt/yang-ai-gateway
cp .env.example .env
openssl rand -hex 32
```

将随机值写入 `.env` 的 `DEVICE_TOKEN`，然后启动：

```bash
docker compose up -d --build
docker compose ps
docker compose logs -f gateway
```

## Nginx

将 `nginx/ai-gateway.conf.example` 中的三个 `location` 合并到 `ai.example.com` 已有的 HTTPS `server` 块中，然后：

```bash
sudo nginx -t
sudo systemctl reload nginx
curl https://ai.example.com/health
curl -X POST https://ai.example.com/robot/ota/
```

不要直接覆盖现有 Nginx 配置，以免影响微信小程序与博客。

## ESP32

在 `idf.py menuconfig` 中将 `Xiaozhi Assistant -> Default OTA URL` 设置为：

```text
https://ai.example.com/robot/ota/
```

重新编译和烧录。设备启动会请求 OTA；按键开始会话后，服务日志应依次出现：

```text
OTA request
Device connected
Device hello
Handshake completed
Listen event
Opus received
```

看到 `Opus received` 即表示第一阶段传输闭环完成。
