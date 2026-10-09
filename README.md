# tg2drive

用 GitHub Actions 将 Telegram 附件转存至 OneDrive / SharePoint / Google Drive。支持多路下载、顺序分片上传、自动资源缓冲、TG 进度通知和 Gist 加密登录缓存。

## 使用

1. 在仓库 **Settings → Secrets and variables → Actions** 配置下表；本地运行填写 `.ven.local`。
2. 给机器人发送 `/start`，在 Actions 中运行 **Telegram to SharePoint / OneDrive**。
3. 无有效登录缓存时，打开机器人发来的授权链接，在本地浏览器登录，将地址栏完整回调 URI 发回机器人（也可发送 `/oauth 完整URI`）。
4. 将附件发送或转发给机器人，回复附件 `/select`；完成后机器人返回文件链接。

工作流启动时自动设置你的私聊命令菜单，输入 `/` 可点选 `/start`、`/select`、`/oauth`；菜单会保留，但命令仅在工作流运行期间响应。`/select` 仍需回复附件，`/oauth` 后需添加完整回调 URI。

仅指定用户的私聊可选择附件。回调 URI 包含授权码，程序通过 OAuth + PKCE 换取 token。Gist 缓存有效时自动登录，不需要手工填写刷新 token。

## 环境变量

凭据和目标地址存入 Repository secrets；并发数、等待秒数存入 Repository variables。本地使用同名环境变量。

### Telegram

| 变量 | 位置 | 获取步骤 |
| --- | --- | --- |
| `TG_BOT_API_ID` | Secret | [my.telegram.org](https://my.telegram.org/apps) → API development tools → 创建应用 → api_id |
| `TG_BOT_API_HASH` | Secret | 同上，复制 api_hash |
| `TG_BOT_TOKEN` | Secret | [BotFather](https://t.me/BotFather) → `/newbot` → 复制 Token |
| `TG_BOT_CREATOR_ID` | Secret | 私聊机器人发消息，通过 [getUpdates](https://core.telegram.org/bots/api#getupdates) 读取 `message.from.id`；程序运行前获取 |

### 微软 OAuth

| 变量 | 位置 | 获取步骤 / 默认值 |
| --- | --- | --- |
| `CLIENTID` | Secret | [Entra 管理中心](https://entra.microsoft.com/) → 应用注册 → 新注册 → 概述 → 应用程序（客户端）ID |
| `CLIENTSECRET` | Secret | 应用注册 → 证书和密码 → 新建客户端密码 → 复制 **Value（值）**；不是 Secret ID。Web 客户端必填，桌面公共客户端留空 |
| `TENANTID` | Secret | 应用概述 → 目录（租户）ID；可留空，默认 `common` |
| `OAUTH_REDIRECT_URI` | Secret | 应用 → 身份验证 → 添加平台及回调地址；默认 `http://localhost`，必须与注册值一致 |
| `OAUTH_WAIT_SECONDS` | Variable | 自行设置，默认 `600`，范围 `30–1800` 秒 |

应用 API 权限添加 Microsoft Graph **委托权限** `Files.ReadWrite.All`、`Sites.Read.All`，按组织要求完成管理员同意。回调平台：有密钥使用 **Web**；无密钥使用 **移动和桌面应用**。不使用 SPA 或隐式授权。登录 SharePoint 时使用有文档库写入权限的组织账号。

登录后 localhost 页面无法打开也可复制地址栏完整 URI。参考：[微软 OAuth 授权码流程](https://learn.microsoft.com/en-us/entra/identity-platform/v2-oauth2-auth-code-flow)。

### Gist 缓存（可选）

| 变量 | 位置 | 获取步骤 |
| --- | --- | --- |
| `GIST_ID` | Secret | [创建 Gist](https://gist.github.com/) → 文件名 `tg2drive-token.json`、内容 `{}` → Create secret gist → 复制 URL 最后的 ID |
| `GIST_TOKEN` | Secret | [创建 classic PAT](https://github.com/settings/tokens/new) → 勾选 `gist` → 生成并复制 Token；或使用有 Gists 读写权限的 fine-grained PAT |
| `GIST_ENCRYPTION_KEY` | Secret | 安装依赖后执行下方命令生成；本地与 Actions 使用同一密钥，并长期保留 |

```powershell
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

刷新 token 加密后存入 Gist，刷新成功后自动更新。不存在、过期或撤销时重新验证；未配置 `GIST_ID` 时每次验证。Gist 网络、权限或解密错误会直接报错。

### SharePoint（可选）

| 变量 | 位置 | 获取步骤 |
| --- | --- | --- |
| `SHAREPOINT_SITE_URL` | Secret | 从浏览器复制**站点地址**，如 `https://tenant.sharepoint.com/sites/site-name`；不要复制分享链接或文档库页面链接 |
| `SHAREPOINT_SITE_ID` | Secret | [Graph Explorer](https://developer.microsoft.com/en-us/graph/graph-explorer) → `GET /v1.0/sites/{hostname}:/{site-path}` → 复制 `id`；根站点用 `GET /v1.0/sites/{hostname}` |
| `SHAREPOINT_DRIVE_ID` | Secret | Graph Explorer → `GET /v1.0/sites/{site-id}/drives` → 找到目标文档库，复制其 `id` |

三项不必全部填写。优先级：Drive ID → Site ID → Site URL → 登录用户的 OneDrive。Site ID / URL 使用站点默认文档库。目标文件夹路径相对于文档库根目录，不包含文档库名称。文件沿用现有访问权限，不自动创建匿名分享。

### Google Drive（可选）

所有项目同样填入 Repository secrets；本地填 `.ven.local`。默认仍为 OneDrive。

| 变量 | 位置 | 获取步骤 / 默认值 |
| --- | --- | --- |
| `STORAGE_PROVIDER` | Secret | `onedrive`（默认）或 `google`；选择 `google` 后无需微软客户端配置 |
| `GOOGLE_CLIENT_ID` | Secret | [Google Cloud Console](https://console.cloud.google.com/) → 新建项目 → 启用 [Google Drive API](https://console.cloud.google.com/apis/library/drive.googleapis.com) → [Google Auth Platform](https://console.cloud.google.com/auth/clients) → 创建 **桌面应用** OAuth 客户端 → 客户端 ID |
| `GOOGLE_CLIENT_SECRET` | Secret | 同一桌面客户端 → 下载 JSON → 复制 `client_secret` |
| `GOOGLE_REDIRECT_URI` | Secret | 默认 `http://localhost:8080`，桌面应用使用 localhost 回调；登录后复制地址栏完整 URI 发回 TG |
| `GOOGLE_FOLDER_ID` | Secret | 可留空（我的云端硬盘根目录）；指定文件夹时，复制 `https://drive.google.com/drive/folders/文件夹ID` 的最后一段 |

在 Google Auth Platform 配置受众及测试用户，添加范围 `https://www.googleapis.com/auth/drive`（用于定位已有目标文件夹并创建子目录）。外部应用处于 Testing 时，刷新 token 通常 7 天过期；正式使用需按 Google 要求发布应用及处理权限验证。参考：[Google OAuth](https://developers.google.com/identity/protocols/oauth2/native-app)。

`TARGET_FOLDER` 在 `GOOGLE_FOLDER_ID` 下创建或复用。支持有写入权限的共享云端硬盘目录；同名文件新建，不覆盖。Google 登录缓存使用同一 Gist 的独立加密文件 `tg2drive-google-token.json`，不会替换微软缓存。上传使用 [Google resumable upload](https://developers.google.com/workspace/drive/api/guides/manage-uploads)，每片 10 MiB，顺序上传，不创建公开分享链接。

### 转存设置

| 变量 | 位置 | 设置方式 / 默认值 |
| --- | --- | --- |
| `TARGET_FOLDER` | Secret | 自行设置目标文件夹，默认 `Public/Telegram`；用 `/` 分隔，支持自动创建目录 |
| `WAIT_SECONDS` | Variable | 等待回复 `/select` 的时长，默认 `300`，范围 `30–1800` 秒 |
| `DOWNLOAD_WORKERS` | Variable | 下载并发上限，范围 `1–8`，默认 `4`；实际并发按可用资源调整 |

目录从 Secrets 读取，等待秒数和下载并发从 Variables 读取；手动运行不再填写配置。

## 运行行为

- Telegram 每次请求 512 KiB，累计为 10 MiB 分片；下载并行，OneDrive 按文件顺序上传。
- 开始转存时按可用内存和临时磁盘计算并发及缓冲，磁盘缓存最多 1 GiB；分片上传后删除，任务结束清理缓存。
- TG 每 10 秒更新下载 / 上传进度、速度和预计剩余时间，日志不输出 token 或完整 OAuth 回调。
- 同一个仓库的转存任务串行执行；单次最多 180 分钟，中断后重跑从头开始。
- 非空文件同名时自动重命名；零字节文件同名时会覆盖。附件需对机器人可见，暂不支持直接输入频道消息链接。

转存工作流结束后（成功、失败或取消），独立清理工作流自动删除该次运行记录及日志。清理工作流自身的记录会保留；排查转存问题请查看 TG 提示。

## 本地运行

```powershell
python -m pip install -r requirements.txt
python transfer.py
```

程序自动读取 `.ven.local`，已有环境变量优先。该文件被 Git 忽略，不要提交凭据。

验证：`python -m unittest discover -s tests -v`。
