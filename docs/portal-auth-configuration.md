# 用户登录弹窗：配置与启用

主页 `/design-demo` 和论文页 `/design-demo/papers` 共用同一个登录弹窗。手机号、邮箱验证码和 Google、微信、QQ 的接入代码已实现；未提供配置时显示“暂未开放”，不会生成演示验证码、模拟二维码或虚假的登录状态。

## 待配置项

以下值供 Python API 进程读取，可放在项目根目录 `.env`，然后重启服务。密钥不能放在 `NEXT_PUBLIC_*` 变量中，也不应提交到 Git。

| 类别 | 需要准备的内容 | 配置项 |
| --- | --- | --- |
| 公共基础 | 至少 32 个字符的随机密钥、真实发布的用户协议及隐私政策地址、网站正式域名 | `NEWSROOM_AUTH_CHALLENGE_SECRET`、`NEWSROOM_AUTH_TERMS_URL`、`NEWSROOM_AUTH_PRIVACY_URL`、`NEWSROOM_AUTH_PUBLIC_ORIGIN` |
| 邮箱 | SMTP 服务器、端口、已验证的发件人、账号与授权码 | `NEWSROOM_AUTH_SMTP_HOST`、`NEWSROOM_AUTH_SMTP_PORT`、`NEWSROOM_AUTH_SMTP_FROM`、`NEWSROOM_AUTH_SMTP_USERNAME`、`NEWSROOM_AUTH_SMTP_PASSWORD`、`NEWSROOM_AUTH_SMTP_SECURITY` |
| 手机号 | Twilio 账号及可发送短信的号码 | `NEWSROOM_AUTH_TWILIO_ACCOUNT_SID`、`NEWSROOM_AUTH_TWILIO_AUTH_TOKEN`、`NEWSROOM_AUTH_TWILIO_FROM` |
| Google | Google 网站登录应用和回调地址 | `NEWSROOM_AUTH_GOOGLE_CLIENT_ID`、`NEWSROOM_AUTH_GOOGLE_CLIENT_SECRET` |
| 微信 | 微信开放平台的网站应用和回调域名 | `NEWSROOM_AUTH_WECHAT_CLIENT_ID`、`NEWSROOM_AUTH_WECHAT_CLIENT_SECRET` |
| QQ | QQ 互联的网站应用和回调域名 | `NEWSROOM_AUTH_QQ_CLIENT_ID`、`NEWSROOM_AUTH_QQ_CLIENT_SECRET` |

需要先配置随机密钥和两个已发布的协议地址，才能启用用户注册。登录弹窗中的协议勾选、链接会随这些配置出现，不生成虚构的法律文本。只配置需要开放的登录方式，其余继续关闭。

前端沿用 `NEWSROOM_API_BASE_URL` 连接真实 API；如果后端启用了服务 API 鉴权，还需配置既有的 `NEWSROOM_API_TOKEN`。上线使用 HTTPS，同一个正式域名完成登录和回调。普通用户登录不会替代管理员 API 的权限配置。

## 交互与使用边界

- 手机号、邮箱使用 6 位验证码，有效期 10 分钟；SMTP 或短信服务接受发送请求后才显示已发送，实际送达仍需服务商验收。
- Google、QQ 使用授权窗口；窗口被拦截时可以整页授权，返回后恢复研究问题。微信加载官方扫码页面，未配置时没有占位二维码。
- 同一个第三方身份重复登录会复用账号；不同登录方式不会按昵称或邮箱自动合并，目前没有账号绑定功能。
- 当前短信适配器是 Twilio，尚未实现阿里云或腾讯云适配器；后续可通过独立的 `OtpDelivery` 接口接入选定的服务商。开通前需确认目标地区发送支持、号码要求和费用。
- 数据使用原有文件锁和原子写入，适用于共享同一文件系统的单机部署；多主机需先接入共享事务存储。手机、邮箱身份数据要限制文件权限并纳入备份。
- OTP/OAuth 事务在后续写入时清理 24 小时以前的记录；OAuth 每个浏览器每小时最多 30 次、全局最多 500 次。
- 当前尚未提供外部应用或发送服务凭据。离线测试和浏览器检查通过不代表真实短信、邮件或第三方授权已经开通；下方列出精确配置和启用步骤。

## 详细配置参考

The portal login dialog is fail-closed. A method is advertised as available only when its delivery or provider configuration, a challenge secret, and both published legal URLs are present. Development and automated tests do not emit verification codes or create simulated provider sessions.

## Common configuration

Set these values on the Python API process. Provider secrets must not be exposed through `NEXT_PUBLIC_*` variables or returned by `/api/v1/auth/methods`.

| Variable | Purpose |
| --- | --- |
| `NEWSROOM_AUTH_CHALLENGE_SECRET` | Random secret of at least 32 characters used for keyed browser, destination, code, and state digests. Generate independently from API and session credentials. |
| `NEWSROOM_AUTH_TERMS_URL` | Published absolute HTTP(S) user-terms URL. |
| `NEWSROOM_AUTH_PRIVACY_URL` | Published absolute HTTP(S) privacy-policy URL. |
| `NEWSROOM_AUTH_PUBLIC_ORIGIN` | Canonical portal origin, for example `https://research.example.com`, with no path, query, or fragment. |
| `NEWSROOM_AUTH_OTP_PATH` | Optional single-host OTP transaction file. Defaults to `.newsroom/auth/otp_challenges.json`. |
| `NEWSROOM_AUTH_OAUTH_PATH` | Optional single-host OAuth state file. Defaults to `.newsroom/auth/oauth_states.json`. |
| `NEWSROOM_AUTH_USERS_PATH` | Existing user and provider-qualified identity store. Defaults to `.newsroom/auth/users.json`. |
| `NEWSROOM_AUTH_SESSIONS_PATH` | Existing session store. Defaults to `.newsroom/auth/sessions.json`. |

The two legal URLs are required before any public account can be created. Partial or malformed configuration keeps the affected method unavailable. Existing administrator bootstrap and username/password login remain separate.

## Email verification

The implementation sends a six-digit, ten-minute code through authenticated SMTP and reports success only after the SMTP server accepts the message.

| Variable | Required | Description |
| --- | --- | --- |
| `NEWSROOM_AUTH_SMTP_HOST` | yes | SMTP server hostname. |
| `NEWSROOM_AUTH_SMTP_PORT` | no | Defaults to `587`. |
| `NEWSROOM_AUTH_SMTP_FROM` | yes | Verified sender address. |
| `NEWSROOM_AUTH_SMTP_USERNAME` | provider-specific | SMTP username. |
| `NEWSROOM_AUTH_SMTP_PASSWORD` | provider-specific | SMTP password or application credential. |
| `NEWSROOM_AUTH_SMTP_SECURITY` | no | `starttls` (default) or `ssl`. |
| `NEWSROOM_AUTH_SMTP_ALLOW_UNAUTHENTICATED` | no | Set to `true` only for an operator-controlled SMTP relay that intentionally uses no authentication. |

Production deployments should use TLS with a provider-verified sender and should configure SPF, DKIM, and DMARC for the sender domain. Do not use a console mail backend in a public deployment.

## Phone verification

SMS delivery uses Twilio's Messages REST endpoint. The application generates and stores only a keyed digest of the code; Twilio receives the rendered code in the outgoing message.

| Variable | Required | Description |
| --- | --- | --- |
| `NEWSROOM_AUTH_TWILIO_ACCOUNT_SID` | yes | Twilio account SID. |
| `NEWSROOM_AUTH_TWILIO_AUTH_TOKEN` | yes | Twilio auth token. |
| `NEWSROOM_AUTH_TWILIO_FROM` | yes | Twilio-owned E.164 sender number. |

Destination numbers must be supplied in E.164 form. Trial-account restrictions, regional sender registration, and local messaging regulations remain operator responsibilities. A failed Twilio request is exposed only as a stable delivery error; upstream bodies and credentials are not returned.

## Google, WeChat, and QQ

Register each callback exactly as shown, replacing the example origin with `NEWSROOM_AUTH_PUBLIC_ORIGIN`:

```text
https://research.example.com/api/auth/oauth/google/callback
https://research.example.com/api/auth/oauth/wechat/callback
https://research.example.com/api/auth/oauth/qq/callback
```

Configure only providers that have an approved application and matching callback registration:

| Provider | Client ID | Client secret |
| --- | --- | --- |
| Google | `NEWSROOM_AUTH_GOOGLE_CLIENT_ID` | `NEWSROOM_AUTH_GOOGLE_CLIENT_SECRET` |
| WeChat website application | `NEWSROOM_AUTH_WECHAT_CLIENT_ID` | `NEWSROOM_AUTH_WECHAT_CLIENT_SECRET` |
| QQ Connect website application | `NEWSROOM_AUTH_QQ_CLIENT_ID` | `NEWSROOM_AUTH_QQ_CLIENT_SECRET` |

Google uses OpenID Connect authorization code flow. The API validates the signed ID token, issuer, audience, expiry, nonce, and subject through Authlib before creating a session. WeChat and QQ resolve their provider-issued `openid`. Accounts are keyed by `(provider, subject)`; profile email or display name never merges accounts.

Authorization state is short-lived, browser-bound, stored as a keyed digest, and consumed before the provider code is exchanged. A failed or interrupted exchange requires a new authorization start. Access tokens are neither persisted nor returned to the browser. Authorization codes travel through the provider callback URL and are exchanged only on the server; production access logs should redact callback query strings.

## Limits and persistence boundary

OTP requests have a 60-second cooldown, a maximum of five sends per browser per hour, three sends per destination per hour, and one hundred sends globally per hour. Verification is capped at five guesses. OAuth state expires after five minutes. These limits are enforced using file locks and therefore cover one shared filesystem host only; multi-host deployment requires a transactional shared implementation before enabling public methods.

Public identities always create the `user` role. They cannot initialize or acquire the `admin` role. Existing version-1 user records remain readable; the next write upgrades the file to version 2 by adding provider identities.

## Activation checklist

1. Initialize the administrator through the existing private bootstrap flow.
2. Publish the terms and privacy pages and set their canonical URLs.
3. Generate the challenge secret and restrict access to all authentication state files.
4. Configure and verify SMTP and/or Twilio delivery in the intended region.
5. Register exact HTTPS callback URLs and configure only approved OAuth applications.
6. Restart both API and portal processes, then confirm `/api/v1/auth/methods` exposes only intended methods.
7. Run one operator-approved live test for every enabled method, including cancellation, expiry, replay, and logout.

Offline unit and contract tests validate protocol boundaries with injected providers and delivery adapters. They do not prove that external accounts, sender registration, DNS records, callback approval, or production delivery are active.
