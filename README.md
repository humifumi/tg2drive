# tg2drive

通过 GitHub Actions 将 Telegram 附件分片转存至 OneDrive。Telegram 生产者每下载 10 MiB 就放入有界队列，OneDrive 单消费者严格顺序上传；上传当前片时可同时下载下一片。最多保留两片，文件缓冲约 20 MiB，不需要完整落盘。末片可以不足 10 MiB；完成后从 OneDrive 下载到自己的硬盘，或通过 OneDrive 客户端同步。

## 使用

1. 将本目录上传到自己的 GitHub 仓库，建议使用私有仓库。
2. 在 Settings → Secrets and variables → Actions 添加下表中的 Secrets。
3. 在 Telegram 打开机器人私聊，先发送 `/start`。
4. 在 GitHub Actions 选择 **Telegram to SharePoint / OneDrive** → **Run workflow**。目录默认 `Public/Telegram`，支持自动创建多级目录。
5. 仅使用 OAuth：无有效 Gist 缓存时，机器人发送微软授权链接，在本地浏览器登录后，将地址栏完整回调 URI 发回机器人。授权完成后，再把文件发送或转发给机器人，**回复文件消息** `/select`。支持文件、视频、音频和图片；每次运行转存一个附件。
6. 上传完成后，机器人返回 OneDrive 文件链接。链接需要相应账号访问，不会自动创建公开分享。

机器人只接受指定用户私聊中的 `/select`，其他用户不会触发下载。同一个仓库的工作流串行运行。不要让其他程序同时使用此机器人处理更新。

运行时目录及等待秒数留空，则使用 Variables `TARGET_FOLDER` 和 `WAIT_SECONDS`；OAuth、SharePoint、Gist 的配置由 Secrets / Variables 注入。

## 通过 Telegram 完成 OAuth 登录（默认）

1. 在 Microsoft Entra 应用注册中添加回调 URI `http://localhost`。公共客户端选择“移动和桌面应用”；机密客户端选择“Web”并填写 `CLIENTSECRET`。不使用 SPA 或隐式授权模式。
2. 添加 Graph **委托权限** `Files.ReadWrite.All`、`Sites.Read.All`；如组织要求，完成管理员同意。SharePoint 登录使用有目标文档库写入权限的组织账号。
3. 填写 `CLIENTID` 和 Telegram 配置，`TENANTID` 可填写组织 ID；有有效 Gist 刷新 token 时自动登录，否则通过 TG 验证。
4. 启动程序或 Action，打开机器人发来的授权链接并登录。浏览器跳到 `http://localhost/?code=...&state=...` 后，即使显示无法访问，也复制**完整地址栏 URI**发回机器人；也可发送 `/oauth 完整URI`。
5. 程序校验本次登录 state、回调地址和 PKCE，换取 access token 后继续等待 `/select`。登录等待默认 600 秒。

微软回调 URI 包含一次性授权码，程序在服务端换取 token；不会把 access token 放进回调 URI。access token 只保存在本次运行内存中。配置 Gist 缓存后，refresh token 加密保存于 Gist，下次运行自动刷新；不存在、过期或撤销时才通过 TG 重新验证。

### Gist 登录缓存

配置以下项即可使用缓存：

| 配置 | 存放位置 | 内容 |
| --- | --- | --- |
| `GIST_ID` | Actions Variable | 缓存 Gist ID |
| `GIST_TOKEN` | Actions Secret | 有 Gist 读写权限的 GitHub token（classic PAT 需 gist scope） |
| `GIST_ENCRYPTION_KEY` | Actions Secret | Fernet 加密密钥，本地同名配置 |

Gist 文件 `tg2drive-token.json` 只存密文，不保存 access token。密钥须长期保留，所有使用同一缓存的运行需串行执行。缓存绑定 CLIENTID 和 TENANTID；变更后重新登录。网络错误、权限错误或密钥不匹配会报错，不自动替换缓存。未配置 GIST_ID 时，每次运行都验证。

首次缓存为空，机器人发送登录链接；成功后自动保存刷新 token。之后每次刷新均更新缓存，刷新 token 失效时重新发链接。Gist 请求使用 [GitHub 官方 API](https://docs.github.com/en/rest/gists/gists)。

仅支持 OAuth 用户授权及 Gist 刷新缓存。授权错误不会切换成应用凭据。

协议说明见 [微软授权码流程文档](https://learn.microsoft.com/en-us/entra/identity-platform/v2-oauth2-auth-code-flow)。

## Telegram Secrets

| 名称 | 内容 |
| --- | --- |
| `TG_BOT_API_ID` | 从 https://my.telegram.org 获取的 API ID |
| `TG_BOT_API_HASH` | 对应 API Hash |
| `TG_BOT_TOKEN` | 从 BotFather 创建机器人的 Token |
| `TG_BOT_CREATOR_ID` | 自己的 Telegram 数字用户 ID |

机器人通过 MTProto 下载，不依赖 HTTP Bot API 的文件下载接口。无法转发的受保护消息、机器人看不到的消息暂不支持；本项目不支持直接粘贴频道链接下载。

## SharePoint 公共文档库（当前目标）

工作流默认目标为 `https://humilr.sharepoint.com` 根站点的默认文档库（链接中的 `Shared Documents`），文件存入其下 `Public/Telegram`。`TARGET_FOLDER` 相对于文档库根目录，不需要再写 `Shared Documents`。

使用组织用户 OAuth 授权，登录用户须有文档库写入权限。应用需 Graph 委托权限 `Files.ReadWrite.All`、`Sites.Read.All`。

| 配置 | 说明 |
| --- | --- |
| `SHAREPOINT_SITE_URL` | GitHub Actions **Variable**，默认 `https://humilr.sharepoint.com`；本地同名环境变量 |
| `SHAREPOINT_SITE_ID` | 可选 Secret，跳过站点 URL 解析，使用站点默认文档库 |
| `SHAREPOINT_DRIVE_ID` | 可选 Secret，直接指定文档库，优先级最高 |

优先级：Drive ID → Site ID → Site URL → 原 OneDrive 逻辑。若要恢复 OneDrive 工作流，请去掉 YAML 中 `SHAREPOINT_SITE_URL` 的默认值，并清空三个 SharePoint 配置。

公共目录沿用文档库现有权限，不会创建匿名分享链接。相关端点见 [Microsoft Graph SharePoint 文档](https://learn.microsoft.com/en-us/graph/api/resources/sharepoint?view=graph-rest-1.0)。

## OAuth 应用配置

| 名称 | 内容 |
| --- | --- |
| `CLIENTID` | 应用 Client ID，必填 |
| `CLIENTSECRET` | Web 机密客户端必填；桌面公共客户端留空 |
| `TENANTID` | 组织 Tenant ID，可选，默认 common |

刷新 token 由 Gist 缓存管理，不需要单独配置环境变量。

## 行为与限制

- Telegram 底层每次请求 512 KiB，累计成 10 MiB 分片。下载与上传并行，最多两片在途；缓冲区满时下载等待。OneDrive 单消费者顺序上传，限流及临时网络故障最多尝试 5 次。
- 自动创建目标目录；普通非空文件同名时自动重命名，不清空已有目录。零字节文件使用直接上传，同名时会覆盖。
- 等待选择文件默认 300 秒，可设为 30–1800 秒；工作流最多运行 180 分钟。
- 最多缓存两片文件，不保存完整附件或 session 文件，不上传附件为 GitHub Artifact。任一端失败会停止另一端，等待正在执行的上传请求结束，再尝试取消未完成的 OneDrive 上传会话；重跑从头开始。
- 速度取决于 GitHub Runner 到 Telegram/OneDrive 的线路，不能保证每次都比客户端快。大文件还受账号限速、上传会话有效期和 OneDrive 配额影响。
- Actions 使用量按你的 GitHub 账号额度计费；本地不需要持续开机。

## 本地验证

```powershell
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
```

本地执行 `python transfer.py` 会自动读取同目录 `.ven.local`，已设置的环境变量优先。文件被 Git 忽略。Action 继续从 Secrets / Variables 读取配置。

参考：[Telethon 文档](https://docs.telethon.dev/en/stable/modules/client.html)、[Microsoft Graph 分片上传](https://learn.microsoft.com/graph/api/driveitem-createuploadsession)。
- 日志显示时间、登录/缓存阶段、当前下载和上传状态、分片序号、已上传百分比、平均转存速度及预计剩余时间。百分比按 OneDrive 确认上传的字节计算，平均速度包含下载和上传耗时；Telegram 机器人每 10 秒编辑同一条状态消息，分别显示下载和上传百分比、已传大小、缓冲等待状态、分片完成数、速度及预计剩余时间。下载或上传单片耗时较长时也会更新，消息更新失败不影响文件转存。下载日志最多每 5 秒一次。重试会显示原因和等待时间，不输出 token 或 OAuth 回调 URI。
