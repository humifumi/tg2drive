# tg2drive

通过 GitHub Actions 将 Telegram 附件分片转存至 OneDrive。每下载 10 MiB，就顺序上传该片，确认成功后才下载下一片；最后不足 10 MiB 的部分直接上传。文件缓冲在内存中，不需要完整落盘；完成后从 OneDrive 下载到自己的硬盘，或通过 OneDrive 客户端同步。

## 使用

1. 将本目录上传到自己的 GitHub 仓库，建议使用私有仓库。
2. 在 Settings → Secrets and variables → Actions 添加下表中的 Secrets。
3. 在 Telegram 打开机器人私聊，先发送 `/start`。
4. 在 GitHub Actions 选择 **Telegram to SharePoint / OneDrive** → **Run workflow**。目录默认 `Public/Telegram`，支持自动创建多级目录。
5. 默认 `auth_mode=oauth`：机器人先发送微软授权链接，在本地浏览器登录后，将地址栏完整回调 URI 发回机器人。授权完成后，再把文件发送或转发给机器人，**回复文件消息** `/select`。支持文件、视频、音频和图片；每次运行转存一个附件。
6. 上传完成后，机器人返回 OneDrive 文件链接。链接需要相应账号访问，不会自动创建公开分享。

机器人只接受指定用户私聊中的 `/select`，其他用户不会触发下载。同一个仓库的工作流串行运行。不要让其他程序同时使用此机器人处理更新。

工作流默认 `auth_mode=configured`，读取仓库 Variable `AUTH_MODE`（未设置时为 `oauth`）。运行时目录及等待秒数留空，则分别使用 Variables `TARGET_FOLDER` 和 `WAIT_SECONDS`。`OAUTH_REDIRECT_URI`、`OAUTH_WAIT_SECONDS`、`SHAREPOINT_SITE_URL` 也使用同名 Variables；凭据通过 Secrets 注入。

## 通过 Telegram 完成 OAuth 登录（默认）

1. 在 Microsoft Entra 应用注册中添加回调 URI `http://localhost`。公共客户端选择“移动和桌面应用”；机密客户端选择“Web”并填写 `CLIENTSECRET`。不使用 SPA 或隐式授权模式。
2. 添加 Graph **委托权限** `Files.ReadWrite.All`、`Sites.Read.All`；如组织要求，完成管理员同意。SharePoint 登录使用有目标文档库写入权限的组织账号。
3. 填写 `CLIENTID` 和 Telegram 配置，`TENANTID` 可填写组织 ID；无须预先填写 `REFRESH_TOKEN`。`AUTH_MODE=oauth` 会使用交互登录，忽略已有 refresh token。
4. 启动程序或 Action，打开机器人发来的授权链接并登录。浏览器跳到 `http://localhost/?code=...&state=...` 后，即使显示无法访问，也复制**完整地址栏 URI**发回机器人；也可发送 `/oauth 完整URI`。
5. 程序校验本次登录 state、回调地址和 PKCE，换取 access token 后继续等待 `/select`。登录等待默认 600 秒。

微软回调 URI 包含一次性授权码，程序在服务端换取 token；不会把 access token 放进回调 URI。token 仅保存在本次运行内存中，不回写文件、不发回 Telegram，也不自动保存至 GitHub Secrets；下次运行重新登录。

`AUTH_MODE=auto`（Action 中选择 `auto`）保留原有 refresh token / 应用凭据模式。OAuth 配置错误或超时会结束本次任务，不会自动换成其他账号授权。

协议说明见 [微软授权码流程文档](https://learn.microsoft.com/en-us/entra/identity-platform/v2-oauth2-auth-code-flow)。

## Telegram Secrets

| 名称 | 内容 |
| --- | --- |
| `TG_BOT_API_ID` | 从 https://my.telegram.org 获取的 API ID |
| `TG_BOT_API_HASH` | 对应 API Hash |
| `TG_BOT_TOKEN` | 从 BotFather 创建机器人的 Token |
| `TG_BOT_CREATOR_ID` | 自己的 Telegram 数字用户 ID |

机器人通过 MTProto 下载，不依赖 HTTP Bot API 的文件下载接口。无法转发的受保护消息、机器人看不到的消息暂不支持；本项目不支持直接粘贴频道链接下载。

## OneDrive Secrets

支持两种方式，**存在 `REFRESH_TOKEN` 时优先使用个人授权模式**。沿用参考项目中的变量名。

## SharePoint 公共文档库（当前目标）

工作流默认目标为 `https://humilr.sharepoint.com` 根站点的默认文档库（链接中的 `Shared Documents`），文件存入其下 `Public/Telegram`。`TARGET_FOLDER` 相对于文档库根目录，不需要再写 `Shared Documents`。

使用组织的 `CLIENTID`、`CLIENTSECRET`、`TENANTID`，应用需有文档库写入权限（例如 Graph 应用权限 `Files.ReadWrite.All`，管理员同意）。通过站点 URL 自动解析时，还需 `Sites.Read.All` 权限；直接填写 Drive ID 可跳过站点解析。此模式不需要 `ONEDRIVE_USER_PRINCIPAL_NAME`。也支持有目标站点访问权的组织用户 refresh token。

| 配置 | 说明 |
| --- | --- |
| `SHAREPOINT_SITE_URL` | GitHub Actions **Variable**，默认 `https://humilr.sharepoint.com`；本地同名环境变量 |
| `SHAREPOINT_SITE_ID` | 可选 Secret，跳过站点 URL 解析，使用站点默认文档库 |
| `SHAREPOINT_DRIVE_ID` | 可选 Secret，直接指定文档库，优先级最高 |

优先级：Drive ID → Site ID → Site URL → 原 OneDrive 逻辑。若要恢复 OneDrive 工作流，请去掉 YAML 中 `SHAREPOINT_SITE_URL` 的默认值，并清空三个 SharePoint 配置。

公共目录沿用文档库现有权限，不会创建匿名分享链接。相关端点见 [Microsoft Graph SharePoint 文档](https://learn.microsoft.com/en-us/graph/api/resources/sharepoint?view=graph-rest-1.0)。

### 个人版或组织版：用户授权

在 Microsoft Entra 注册应用，个人版需允许个人 Microsoft 账号登录。添加 Microsoft Graph **委托权限** `Files.ReadWrite`，授权时请求 `offline_access`，通过 OAuth 授权码流程取得 refresh token。

| 名称 | 内容 |
| --- | --- |
| `CLIENTID` | 应用 Client ID |
| `REFRESH_TOKEN` | 用户授权后取得的 refresh token |
| `CLIENTSECRET` | 机密客户端必填；公共客户端可不填 |
| `TENANTID` | 可选，默认 `common` |

每次运行自动换取 access token，不在日志输出令牌。没有自动回写新的 refresh token；如果授权失效或撤销，重新授权并更新 Secret。不要把 token 文件提交到仓库。

### 组织版：应用授权（兼容原项目）

不设置 `REFRESH_TOKEN`。应用需要 Microsoft Graph **应用权限** `Files.ReadWrite.All` 并完成管理员同意。

| 名称 | 内容 |
| --- | --- |
| `CLIENTID` | 应用 Client ID |
| `CLIENTSECRET` | 应用 Client Secret |
| `TENANTID` | 组织 Tenant ID |
| `ONEDRIVE_USER_PRINCIPAL_NAME` | 目标用户邮箱/UPN，用户须已开通 OneDrive |

## 行为与限制

- Telegram 底层每次请求 512 KiB，累计到 10 MiB 后暂停下载，上传当前片；不并行下载和上传。OneDrive 限流及临时网络故障最多尝试 5 次。
- 自动创建目标目录；普通非空文件同名时自动重命名，不清空已有目录。零字节文件使用直接上传，同名时会覆盖。
- 等待选择文件默认 300 秒，可设为 30–1800 秒；工作流最多运行 180 分钟。
- 每次仅缓存一片文件，不保存完整附件或 session 文件，不上传附件为 GitHub Artifact。失败时尝试取消未完成的 OneDrive 上传会话；重跑从头开始。
- 速度取决于 GitHub Runner 到 Telegram/OneDrive 的线路，不能保证每次都比客户端快。大文件还受账号限速、上传会话有效期和 OneDrive 配额影响。
- Actions 使用量按你的 GitHub 账号额度计费；本地不需要持续开机。

## 本地验证

```powershell
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
```

本地执行 `python transfer.py` 会自动读取同目录 `.ven.local`，已设置的环境变量优先。文件被 Git 忽略。Action 继续从 Secrets / Variables 读取配置。

参考：[Telethon 文档](https://docs.telethon.dev/en/stable/modules/client.html)、[Microsoft Graph 分片上传](https://learn.microsoft.com/graph/api/driveitem-createuploadsession)。
