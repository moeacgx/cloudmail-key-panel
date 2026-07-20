# CloudMail Key Panel

一个基于 Python + FastAPI 的 CloudMail 配套 Web 面板。

AI/站长交流群：https://t.me/vpsbbq

用途：
- 首页使用兑换卡领取“定制邮箱”或“独立邮箱”，成功收到验证码后才扣除次数
- iCloud 主邮箱可生成一次性的 `name+随机值@icloud.com` 裂变地址；CloudMail 保留别名时严格匹配，丢失 `+别名` 时由主邮箱族独占实时租约安全回退
- 后台用多标签记录 GPT、Claude、Gemini 等平台历史，同一主邮箱可跨平台复用
- 原有查看 Key 功能保留在 `/key-lookup`

## 业务规则

这个项目把“注册收件邮箱”和“CloudMail 查询邮箱”分开保存，并优先从原始信封收件人字段识别真实投递目标。
CloudMail 若把 iCloud 裂变地址归一化成主邮箱，只有当前邮箱族最新的领取记录能启用主邮箱回退；旧记录只保留历史验证码。

## 功能

- 后台登录
- 兑换卡分类、批次生成、每卡可用次数、有效期、批量复制与 UTF-8 TXT 导出
- 公开兑换卡注册台、服务器端领取状态、浏览器近期邮箱入口
- 定制邮箱支持固定地址、iCloud 裂变地址或由用户选择；独立邮箱成功后退出复用池
- 只有本次领取后、收件地址和平台规则均匹配的验证码才会扣次并追加使用标签
- 公开注册台、后台工作台和外部 API 统一写入成功接码流水；展示、复制、人工标签、无验证码跳过和超时不计成功
- 同一封 CloudMail 邮件按邮件 ID 幂等记账，重复刷新不会增加成功次数
- 无验证码跳过不扣次；连续跳过 3 次后默认冷却 15 分钟
- 邮箱多标签、平台发件人/主题过滤规则、可复用/独立/停止复用策略
- 创建查看 Key（支持自定义 Key 或自动生成）
- Key 支持编辑、删除
- 支持为每个 Key 单独设置“CloudMail 查询邮箱”
- 分类拥有稳定数字 ID，大小写或全角写法相同的分类会复用同一个 ID
- 外部 JSON API 可按分类领取注册邮箱、获取最新验证码和完整最新邮件
- SQLite 持久化保存 Key -> 原始收件人邮箱 / 查询邮箱映射
- 前台输入 Key 查看最近邮件
- 自动提取常见数字验证码
- Docker 部署

## 本地开发

```bash
cd /root/cloudmail-key-panel
source /root/hermes-agent/venv/bin/activate
python -m pip install -e '.[dev]'
python -m pytest -q
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

打开：
- 兑换卡注册台：http://127.0.0.1:8000/
- 查看 Key：http://127.0.0.1:8000/key-lookup
- 后台登录：http://127.0.0.1:8000/admin/login

## 环境变量

复制一份：

```bash
cp .env.example .env
```

主要配置：

```env
APP_NAME=CloudMail Key Panel
APP_SECRET_KEY=replace-with-a-random-secret
APP_ADMIN_USERNAME=admin
APP_ADMIN_PASSWORD=replace-with-a-strong-password
APP_PORT=38080
DATABASE_PATH=./data/app.db
CLOUDMAIL_BASE_URL=https://your-cloudmail-domain.example.com
CLOUDMAIL_ADMIN_EMAIL=admin@example.com
CLOUDMAIL_ADMIN_PASSWORD=replace-with-your-cloudmail-password
# 可选：如果你想固定使用现成 token，可以直接填这个
# CLOUDMAIL_API_TOKEN=
LOOKUP_EMAIL_LIMIT=10
REDEMPTION_SESSION_HOURS=24
REDEMPTION_CLAIM_MINUTES=30
REDEMPTION_SKIP_LIMIT=3
REDEMPTION_SKIP_COOLDOWN_MINUTES=15
PUBLIC_RECENT_MAILBOX_LIMIT=20
```

说明：
- `.env` 里的 CloudMail 配置现在只是“默认值 / 启动兜底值”。
- 真正推荐的方式，是登录后台后直接在“CloudMail 配置”表单里填写地址和固定 Token。
- 后台保存后会写入 SQLite，后续查询优先使用后台里保存的配置，不需要反复改 `.env`。


## CloudMail API 对接

项目当前使用了以下接口：

1. `POST /api/public/genToken`
   - 请求体：`email`, `password`
   - 返回：`token`

2. `POST /api/public/emailList`
   - 请求头：`Authorization: <token>`
   - 请求体里使用：
     - `toEmail`：原始收件人邮箱
     - `timeSort=desc`
     - `type=0`
     - `isDel=0`
     - `num=1`
     - `size=<LOOKUP_EMAIL_LIMIT>`

## 外部工作台 API

外部程序使用后台账号进行 HTTP Basic 认证。生产环境必须通过 HTTPS 调用，
不要在明文 HTTP 中发送后台密码。

登录后台后可打开 `/admin/api`，查看当前分类 ID、全部接口说明、cURL 示例，
并在带二次确认的调试台中直接调用真实 API。调试页不会保存或回显后台密码。

每个领取程序还必须携带一个稳定的 `X-Client-ID`。不同客户端的领取状态彼此隔离，
也不会和浏览器注册工作台互相抢占。客户端 ID 可使用字母、数字、点、下划线、冒号和短横线，
最长 128 个字符。

### 1. 查询标签及数字 ID

```bash
curl -u 'admin:your-password' \
  'https://your-panel.example.com/api/v1/categories'
```

新版也可请求 `/api/v1/tags` 获取标签用途、归属邮箱数、真实成功次数、独立账号策略和单邮箱裂变上限。

响应中的 `id` 是稳定标签 ID，`count` 是拥有该标签的主邮箱数量；`/api/v1/tags` 还会返回 `success_count`、`prevent_reuse` 和 `alias_use_limit`（`0` 表示不限）：

```json
{
  "categories": [
    {"id": 1, "name": "未使用", "count": 120},
    {"id": 2, "name": "gpt废号", "count": 30}
  ]
}
```

### 2. 按分类领取邮箱

```bash
curl -u 'admin:your-password' \
  -H 'X-Client-ID: register-worker-01' \
  -H 'Content-Type: application/json' \
  -d '{"category_id":1,"target_tag_id":2,"address_mode":"icloud_alias"}' \
  'https://your-panel.example.com/api/v1/workbench/claim-next'
```

同一个客户端重复调用领取接口时会恢复当前邮箱，不会因为请求重试误跳到下一条。
响应会同时返回：

- `mapping.registration_email`：注册邮箱；
- `mapping.address_mode`：`primary`（固定邮箱）或 `icloud_alias`（全新裂变邮箱）；
- `mapping.tags`：主邮箱已经拥有的标签；
- `mapping.category_id` / `mapping.category`：分类 ID 和名称；
- `latest_code`：按时间倒序找到的最新验证码；
- `latest_email`：最新一封匹配邮件的完整主题、发件人、收件人、时间、HTML `content` 和纯文本 `text`；
- `notice`：共享查询邮箱过滤提示；
- `error`：CloudMail 查询失败信息。

### 3. 查询当前领取

```bash
curl -u 'admin:your-password' \
  -H 'X-Client-ID: register-worker-01' \
  'https://your-panel.example.com/api/v1/workbench/current'
```

### 4. 记录成功接码并领取下一条

`category_id` 是继续领取的来源标签；成功后自动追加领取时 `target_tag_id` 指定的平台标签。
服务端未找到本次领取后的验证码时会返回 `409`，不会写成功流水，也不会改变原有分类。

```bash
curl -u 'admin:your-password' \
  -H 'X-Client-ID: register-worker-01' \
  -H 'Content-Type: application/json' \
  -d '{"mapping_id":123,"category_id":1}' \
  'https://your-panel.example.com/api/v1/workbench/complete'
```

### 5. 跳过当前邮箱并领取下一条

服务端会先查询本次领取后的验证码。没有验证码时只释放领取且不算使用；如果验证码已经到达，
则按领取时的平台标签记录成功接码，不能通过跳过绕开使用记录。

```bash
curl -u 'admin:your-password' \
  -H 'X-Client-ID: register-worker-01' \
  -H 'Content-Type: application/json' \
  -d '{"mapping_id":123,"category_id":1,"address_mode":"icloud_alias"}' \
  'https://your-panel.example.com/api/v1/workbench/skip-current'
```

## Docker 部署

```bash
cd /root/cloudmail-key-panel
cp .env.example .env
# 编辑 .env

docker compose up -d --build
```

默认映射端口：
- 宿主机 `127.0.0.1:38080`
- 容器内 `8000`

也就是只绑定到本机回环地址，不直接暴露公网。
可通过 `.env` 里的 `APP_PORT` 改成其他高位端口。

所以本机访问：
- `http://127.0.0.1:38080/`
- `http://127.0.0.1:38080/admin/login`

## 测试

```bash
cd /root/cloudmail-key-panel
source /root/hermes-agent/venv/bin/activate
python -m pytest -q
python -m compileall app tests
```

## 后续可继续增强

- 兑换卡渠道统计与使用流水分析
- 平台规则测试器和误匹配审计
- 管理员可视化撤销近期邮箱继续接码权限
- Nginx 反代 + HTTPS
