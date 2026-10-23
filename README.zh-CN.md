<div align="center">
  <h1>LookingFor</h1>
  <p><strong>重逢收藏过的好项目，发现下一个心动的仓库。</strong></p>
  <p>从你的 GitHub Stars 出发，探索感兴趣的开源项目。</p>
  <p><a href="README.md">English</a> · <strong>简体中文</strong></p>
  <p>
    <a href="https://repofind.click"><strong>在线体验 ↗</strong></a>
    · <a href="#功能">功能</a>
    · <a href="#演示">演示</a>
    · <a href="#快速开始">快速开始</a>
  </p>
  <p>
    <img src="https://img.shields.io/badge/Python-3.13-48634c?logo=python&logoColor=white" alt="Python 3.13">
    <img src="https://img.shields.io/badge/NiceGUI-3.18-718563" alt="NiceGUI 3.18">
    <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-b3a576" alt="MIT 开源协议"></a>
  </p>
  <img src="assets/readme/hero.png" alt="LookingFor：循着好奇心，探索开源项目" width="100%">
</div>

LookingFor 让你的 GitHub Stars 成为探索的起点。连接账号后，AI 会从收藏中提炼兴趣，帮你分批浏览相关仓库。已经收藏过的项目也会保留，让那些被遗忘的好项目重新出现在你面前。

## 功能

| | 你可以做什么 |
|---|---|
| 🌱 **循着兴趣发现项目** | 根据收藏仓库的描述、topics 和编程语言，生成兴趣搜索关键词。 |
| 🧭 **继续探索下一批** | 分批获取 GitHub 搜索结果，同一次探索中已加载的仓库自动去重。 |
| ⭐ **重新发现旧收藏** | 已收藏和未收藏的项目一起展示，用「已 Star」标记你的旧收藏。 |
| 🔎 **找到更合适的结果** | 按语言筛选已加载的仓库，或搜索名称、描述和 topics。 |
| 📖 **一眼了解项目** | 直接查看简介、topics、语言和 Star 数，点击即可前往 GitHub。 |
| 🌐 **中英文切换** | 从页面顶部切换界面语言，使用自己熟悉的表达。 |
| 🏡 **运行在自己的环境** | 支持本地运行和 Docker 自部署；可以查看同步的收藏、退出登录、删除账号数据。 |

## 演示

<a href="assets/readme/demo.mp4">
  <img src="assets/readme/demo.gif" alt="LookingFor 操作演示：浏览仓库、筛选结果、加载下一批" width="100%">
</a>
<p align="center"><a href="assets/readme/demo.mp4">观看完整视频 ↗</a></p>

<details>
<summary>查看界面截图</summary>

![探索页：兴趣关键词、搜索框与语言筛选](assets/readme/search.jpg)

![仓库卡片：简介、topics、Star 数，以及已收藏和本次新增标记](assets/readme/batch.jpg)

</details>

## 使用方法

1. 打开 [LookingFor](https://repofind.click)，点击「连接 GitHub」。
2. 完成授权，应用会同步你的 Stars 并自动分析兴趣。
3. 在「探索仓库」浏览结果，用搜索框或语言筛选缩小范围，打开感兴趣的项目。
4. 点击「继续发现下一批」获取更多结果，也可以随时在「我的 Stars」浏览已同步的收藏。

在线 beta 需要 GitHub 登录，且 Stars 列表不能为空。「新发现」表示本次浏览最新一批加入的仓库，「已 Star」表示它已在你的收藏中；同一个仓库可以同时拥有两个标记。

## 快速开始

**准备环境：** Python 3.13+、Redis 7+、GitHub App 的 Client ID 和 Client secret，以及 DeepSeek API Key。本地开发不强制使用 Docker；下方启动 Redis 的示例使用 Docker。

### 1. 安装

```bash
git clone https://github.com/Xander-26Code/LookingFor.git
cd LookingFor
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

### 2. 配置

启动 Redis，或使用已有的 Redis 实例：

```bash
docker run --rm -p 127.0.0.1:6379:6379 redis:7-alpine
```

在另一个终端生成会话密钥：

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(48))"
```

填写 `.env`，将生成的密钥用于 `STORAGE_SECRET`：

```dotenv
GITHUB_CLIENT_ID=your_client_id
GITHUB_CLIENT_SECRET=your_client_secret
GITHUB_REDIRECT_URI=http://127.0.0.1:8080/auth/github/callback
STORAGE_SECRET=your_generated_secret
DEEPSEEK_API_KEY=your_deepseek_api_key
REDIS_URL=redis://127.0.0.1:6379/0
```

在 **GitHub App** 设置中，将回调地址设为 `http://127.0.0.1:8080/auth/github/callback`，关闭 Webhook，权限从 **Metadata: read** 开始。如果需要其他账号使用，选择 **Any account**。应用支持刷新过期 token。

浏览器和回调地址统一使用 `127.0.0.1`；复制 `.env.example` 后，请将其中使用 `localhost` 的回调地址替换成上面的值。

### 3. 运行

```bash
python3 main.py
```

打开 [127.0.0.1:8080](http://127.0.0.1:8080)，连接 GitHub，开始探索。

<details>
<summary>更多配置项</summary>

其余本地默认值见 [.env.example](.env.example)。

| 配置项 | 用途 |
|---|---|
| `DATABASE_PATH` | SQLite 文件，默认 `./lookingfor.db` |
| `NICEGUI_STORAGE_PATH` | 服务端会话文件，默认 `./.nicegui` |
| `HOST` / `PORT` | 监听地址，默认 `127.0.0.1:8080` |
| `RELOAD` / `SHOW` | 开发热重载与自动打开浏览器 |
| `LOG_LEVEL` / `UVICORN_LOG_LEVEL` | 应用与服务日志级别 |
| `NICEGUI_REDIS_URL` | 可选的 Redis 会话存储 |
| `DOMAIN` | Docker 部署时供 Caddy 使用的域名 |
| `BACKUP_DIR` | `scripts/backup_db.py` 使用的备份目录 |

重启时保持 `STORAGE_SECRET` 不变。不要将 `.env`、数据库、会话文件、备份和 API Key 提交到 Git。

</details>

## 自部署

Docker Compose 可一起运行 LookingFor、Redis 和 Caddy，提供数据持久化及域名 HTTPS。

按[部署说明](DEPLOYMENT.md)准备域名与生产环境 `.env`，然后启动：

```bash
docker compose up -d --build
```

目前支持**单个应用实例**。部署说明还包含日志查看、健康检查、更新、备份和恢复步骤。

## 常见问题

<details>
<summary>仓库是怎么推荐出来的？</summary>

DeepSeek 从你的 Stars 中提炼兴趣关键词，LookingFor 用这些词搜索 GitHub 公开仓库，合并重复结果后分批展示。目前搜索优先按 Star 数排列；卡片中的关键词表示仓库来自哪些查询组。

</details>

<details>
<summary>可以一直加载更多结果吗？</summary>

只要当前查询还有结果，就可以继续加载。GitHub 每条搜索最多提供 1,000 条可访问结果。LookingFor 会复用公开结果缓存并处理请求冷却；临时请求失败时，已加载的结果会保留，可以稍后重试。

</details>

<details>
<summary>哪些数据会被保存或发送给模型？</summary>

SQLite 保存你的 GitHub 身份信息、同步收藏和分析关键词。DeepSeek 接收选取的仓库元数据及语言、topic 统计；仓库名称和链接可能包含其所有者信息。GitHub token 保存在服务端会话中，不会包含在发给模型的数据里。

可以在应用的「隐私与数据」页面查看详情、删除账号数据。备份有独立的保留周期。当前 beta 的 token 尚未静态加密，自部署时应限制会话存储和备份的访问权限。

</details>

## 参与贡献

欢迎提交问题反馈、推荐体验建议、文档改进和代码 PR。如果结果不符合兴趣，可以提供页面中的搜索关键词，以及你期待发现的仓库示例；请勿附带 token、会话文件或私有数据。

提交代码前运行现有测试：

```bash
python3 -m unittest discover -s tests
```

测试中的 GitHub 和 DeepSeek 请求均使用模拟数据。你可以通过 [Issues](https://github.com/Xander-26Code/LookingFor/issues) 报告问题或讨论改进方向。

## 开源协议

[MIT](LICENSE) © 2026 Haoning Xu。
