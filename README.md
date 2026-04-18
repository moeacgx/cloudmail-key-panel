# CloudMail Key Panel

一个基于 Python + FastAPI 的 CloudMail 配套 Web 面板。

用途：
- 后台绑定“原始收件人邮箱（toEmail）”和一个查看 Key
- 前台用户输入 Key 后，直接查看这个收件人最近收到的验证码邮件
- 会优先展示从主题 / 文本 / HTML 中提取出的验证码

## 业务规则

这个项目按 CloudMail `emailList` 返回的 `toEmail` 作为业务主键，也就是“原始收件人邮箱”。
不是按系统内部转发落地邮箱识别。

## 功能

- 后台登录
- 创建查看 Key（支持自定义 Key 或自动生成）
- SQLite 持久化保存 Key -> 收件人邮箱映射
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
- 前台查询：http://127.0.0.1:8000/
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
```

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

## 推荐部署到 152.53.179.54 的方式

建议流程：

1. 先在本地把项目跑通并测试
2. 提交到 GitHub 仓库
3. 在服务器上拉取仓库
4. 配置 `.env`
5. 执行：

```bash
docker compose up -d --build
```

## 测试

```bash
cd /root/cloudmail-key-panel
source /root/hermes-agent/venv/bin/activate
python -m pytest -q
python -m compileall app tests
```

## 后续可继续增强

- Key 启用 / 禁用
- Key 过期时间
- 管理后台搜索与分页
- JSON API 输出
- 更强的验证码识别规则
- Nginx 反代 + HTTPS
