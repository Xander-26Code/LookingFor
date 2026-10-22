# LookingFor 部署说明

当前版本按**单实例**设计：NiceGUI + SQLite + Redis。不要直接开多个 app 容器/worker；SQLite 和分析任务状态目前是单机模型。

## 1. 准备 GitHub App

1. 在 GitHub → Settings → Developer settings → GitHub Apps 找到你的 App。
2. 记下 **Client ID**，生成 **Client secret**。
3. `Identifying and authorizing users` → Callback URL 填：
   `https://你的域名/auth/github/callback`
4. 权限先只给 `Metadata: read`；不要给 repo 写权限。
5. Webhook 暂时关闭（当前代码没有 webhook 路由）。
6. 测试用户需要能访问 App：`Where can this GitHub App be installed?` 选 `Any account`。
7. token 过期策略：
   - 小范围 beta：`Optional Features` → `User-to-server token expiration` → Opt-out；
   - 公开前建议 Opt-in，并确认 refresh token 流程正常。

GitHub App 的用户 token 默认 8 小时过期；代码已支持 refresh token（需要 App 返回 refresh token）。

## 2. 服务器

推荐国内云香港/新加坡轻量服务器（支付宝/微信可付），Ubuntu 24.04，2C2G 起。

安全组开放：

```text
22   SSH
80   HTTP（Caddy 申请证书）
443  HTTPS
```

安装 Docker：

```bash
curl -fsSL https://get.docker.com | sh
```

## 3. 域名

没有域名可以先用 `sslip.io`：

```text
服务器 IP 1.2.3.4 → 使用 1-2-3-4.sslip.io
```

Caddy 会自动申请 Let's Encrypt 证书。也可以买一个域名，A 记录指向服务器 IP。

## 4. 配置

在服务器上：

```bash
git clone <你的仓库地址> lookingfor
cd lookingfor
cp .env.example .env
```

编辑 `.env`：

```dotenv
GITHUB_CLIENT_ID=...
GITHUB_CLIENT_SECRET=...
GITHUB_REDIRECT_URI=https://1-2-3-4.sslip.io/auth/github/callback
STORAGE_SECRET=<python3 -c "import secrets; print(secrets.token_urlsafe(48))">
DEEPSEEK_API_KEY=...
REDIS_URL=redis://redis:6379/0
DATABASE_PATH=/data/lookingfor.db
NICEGUI_STORAGE_PATH=/data/.nicegui
HOST=0.0.0.0
PORT=8080
RELOAD=0
SHOW=0
DOMAIN=1-2-3-4.sslip.io
BACKUP_DIR=/data/backups
```

注意：

- `STORAGE_SECRET` 必须固定；更改会让所有登录会话失效。
- `DOMAIN` 只给 Caddy 用。
- `GITHUB_REDIRECT_URI` 必须和 GitHub App 的 Callback URL 完全一致。

## 5. 启动

```bash
docker compose up -d --build
docker compose ps
```

查看日志：

```bash
docker compose logs -f app
docker compose logs -f caddy
```

健康检查：

```bash
curl -fsS https://你的域名/health
```

返回：

```json
{"status":"ok","database":"ok","redis":"ok"}
```

如果 Redis 不可用，会返回 `degraded` 但 HTTP 200；数据库不可用返回 503。

## 6. 更新

```bash
git pull
docker compose up -d --build
```

数据在 Docker volume `app-data` 中，容器重建不会丢失 SQLite 和 `.nicegui`。

## 7. 备份与恢复

手动备份：

```bash
docker compose exec app python scripts/backup_db.py
```

或宿主机：

```bash
DATABASE_PATH=/data/lookingfor.db BACKUP_DIR=/data/backups \
  docker compose exec -T app python scripts/backup_db.py
```

建议每天 cron：

```cron
0 3 * * * cd /opt/lookingfor && docker compose exec -T app python scripts/backup_db.py >> /var/log/lookingfor-backup.log 2>&1
```

默认保留最近 7 份（`BACKUP_KEEP`）。备份使用 SQLite 在线备份 API，包含 WAL 数据。

恢复：

```bash
# 1. 先停应用，避免恢复过程中继续写入
docker compose stop app

# 2. 把 /data/backups 里的备份覆盖回 /data/lookingfor.db
#    把 lookingfor-YYYYMMDD-HHMMSS.db 替换成实际文件名
docker compose run --rm app sh -c \
  'cp /data/backups/lookingfor-YYYYMMDD-HHMMSS.db /data/lookingfor.db && rm -f /data/lookingfor.db-wal /data/lookingfor.db-shm'

# 3. 重新启动并检查
docker compose start app
curl -fsS https://你的域名/health
```

如果备份在宿主机，先用 `docker compose cp ./backups/文件名 app:/data/backups/` 复制进容器卷，再执行上面的覆盖命令。生产操作前先停 app，避免同时写入；恢复后执行 `/health` 验证。

## 8. 安全与隐私

- `.env` 不进 Git，服务器上 `chmod 600 .env`。
- `docker compose config` 会把 `.env` 的值展开，**不要公开粘贴它的输出**。
- GitHub token 存在服务端会话存储（`.nicegui` 或 Redis），不下发到页面；beta 环境要限制服务器/Redis 访问。
- 搜索缓存只保存公开仓库结果。
- 用户可在 `/privacy` 查看数据说明，在 `/account/delete` 删除数据，在 `/auth/logout` 退出登录。
- 公开前建议再加：token 加密、会话轮换、审计日志和备份加密。

## 9. 平台替代方案

不想自己维护 Docker 时：

- Sealos / Zeabur：支持 Docker、持久化卷和 Redis，付款前确认是否支持支付宝/微信。
- 本机 + Cloudflare Quick Tunnel：零成本 demo，但 URL 每次变化，适合 1–2 人，不适合长期 beta。
- Render/Fly.io：需要境外卡，当前不作为首选。

## 10. 验收

- [ ] GitHub 登录成功
- [ ] Stars 同步成功
- [ ] 分析成功，刷新后状态可恢复
- [ ] 两个账号数据互不影响
- [ ] 搜索和“继续发现下一批”正常
- [ ] `/health` 返回 ok
- [ ] 重启容器后数据仍在
- [ ] 备份文件可打开并包含用户数据
- [ ] 退出登录、删除数据、隐私页可用
