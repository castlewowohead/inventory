# Inventory 服务器部署（单机演示）

本文按 **一台 Linux 服务器 + 一个域名** 写，部署范围是本仓库的 `backend/` 和 `frontend/`。它适合给导师访问和试用；目前不是多租户生产方案。示例路径为 `/srv/inventory`，域名为 `inventory.example.com`，执行前替换成自己的值。以下命令在服务器的 Bash 中执行，`YOUR_USER` / `YOUR_GROUP` 换成实际的非 root 登录用户名及其主用户组。

项目目前用 SQLite、服务器本地文件存储、Flask API、uni-app H5 前端。后端没有应用级登录或权限隔离；**必须在网站入口同时保护前端、`/api/` 和 `/health`**，并使用 HTTPS。下面采用 Nginx Basic Auth，账号密码仅用于这台演示站点。不要直接把 `python app.py` 或 5000 端口暴露到公网；Flask 开发服务器也不用于部署。[Flask 部署说明](https://flask.palletsprojects.com/en/stable/deploying/)

## 1. 准备服务器和源码

准备有 sudo 权限的 Linux 服务器、指向该服务器的域名、Python **3.13**、Node.js **24** 和 npm。3.13 / 24 是当前项目本地验证过的版本；若使用其他版本，先重新安装依赖并跑构建和测试。Ubuntu 系统还需 Git、Nginx、`htpasswd`：

```bash
sudo apt update
sudo apt install -y git nginx apache2-utils
python3.13 --version
node --version
npm --version
```

先把 `backend/`、`frontend/` 和本文件推到同一个 GitHub 仓库。根目录另有两个独立项目 `kitchen-ledger/`、`restaurant-erp/`，本部署不使用它们。若仓库是 Private，服务器须有读取权限；可给仓库配置只读 [GitHub Deploy Key](https://docs.github.com/en/authentication/connecting-to-github-with-ssh/managing-deploy-keys)。不要把 GitHub 密码或令牌写进命令、文档或 `.env`。

```bash
sudo install -d -m 755 -o "$USER" -g "$(id -gn)" /srv/inventory
git clone git@github.com:OWNER/REPO.git /srv/inventory
cd /srv/inventory
```

若前一轮按 `git add backend frontend` 提交，记得再执行 `git add backend/docs/deploy.md` 并推送，否则服务器 clone 后看不到本文件。

## 2. 安装后端并配置数据库

在 `backend/` 下创建虚拟环境，安装运行依赖及 Gunicorn。项目的 `requirements.txt` 尚未包含 Gunicorn，因此这里单独安装；发布前应记录实际安装版本。Gunicorn 用来加载 `app.py` 导出的 `app`，不运行 Flask 开发服务器。[Flask 的 Gunicorn 用法](https://flask.palletsprojects.com/en/stable/deploying/gunicorn/)

```bash
cd /srv/inventory/backend
python3.13 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m pip install gunicorn
cp .env.example .env
chmod 600 .env
```

编辑 `backend/.env`，至少设定：

```dotenv
APP_ENV=production
SECRET_KEY=换成独立生成的随机值
DATABASE_URL=sqlite:///var/inventory.db
FILE_STORAGE_DIR=var/uploads
UPLOAD_MAX_BYTES=20971520
OCR_PROVIDER=fake
OCR_WORKER_MODE=thread
OCR_WORKER_THREADS=2
HOST=127.0.0.1
PORT=5000
```

可用 `.venv/bin/python -c 'import secrets; print(secrets.token_hex(32))'` 生成 `SECRET_KEY`。`.env.example` 中有重复的 `OCR_PROVIDER=fake` 示例行，编辑时只保留一个最终值。`APP_ENV=production` 会拒绝占位 `SECRET_KEY`；`.env`、`backend/var/`、上传原件和 SQLite 数据库都不能推到 GitHub。

没有真实 OCR 凭据时保留 `OCR_PROVIDER=fake`：网站可演示上传、手工录入及审核，但识别结果是明确标识的演示数据。需要真实识别时，在服务器的 `.env` 中改为 `OCR_PROVIDER=dashscope`，并配置 `OCR_API_KEY`、`OCR_BASE_URL`；后者取自所用工作空间的 API Host，不能猜默认地址。密钥只交给服务器，不写入仓库。详见 [后端配置说明](../README.md)。

先迁移数据库。相对 SQLite 路径会按 `backend/` 解析，默认数据库落在 `backend/var/inventory.db`；原件落在 `backend/var/uploads/`。

```bash
.venv/bin/python -m alembic upgrade head
```

## 3. 构建前端

```bash
cd /srv/inventory/frontend
npm ci
npm run typecheck
npm run build:h5
test -f dist/build/h5/index.html
```

Nginx 稍后直接读取 `frontend/dist/build/h5/`。`vite.config.ts` 的 5173 端口及 `/api` 开发代理只用于本地开发；线上由 Nginx 在同一域名下提供页面并转发 `/api/`、`/health`，前端不用改成跨域请求。

## 4. 用 systemd 运行后端

创建 `/etc/systemd/system/inventory.service`，将 `YOUR_USER`、`YOUR_GROUP` 替换为拥有 `/srv/inventory` 的 Linux 用户及其主用户组（可用 `id -un`、`id -gn` 查看）：

```ini
[Unit]
Description=Inventory Flask API and OCR worker
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=YOUR_USER
Group=YOUR_GROUP
WorkingDirectory=/srv/inventory/backend
Environment=PYTHONUNBUFFERED=1
ExecStart=/srv/inventory/backend/.venv/bin/gunicorn --workers 1 --threads 4 --timeout 120 --bind 127.0.0.1:5000 --access-logfile - --error-logfile - app:app
Restart=on-failure
RestartSec=5
TimeoutStopSec=60

[Install]
WantedBy=multi-user.target
```

`app.py` 在 Gunicorn 导入时读取 `.env`；真实 OCR 且 `OCR_WORKER_MODE=thread` 时，还会在**同一进程**启动识别线程。当前 SQLite 任务领取锁只在进程内有效，因此这里固定 **一个 Gunicorn worker**、不加 `--preload`，也不另起 `python -m src.worker`。增加 worker 进程或独立 worker 前，必须先改造跨进程任务领取逻辑，否则可能重复识别同一页并重复计费。线程数 `--threads 4` 仅用于处理 HTTP 请求，与 `OCR_WORKER_THREADS` 不同。

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now inventory
sudo systemctl status inventory --no-pager
curl -f http://127.0.0.1:5000/health
```

如启动失败，先看 `journalctl -u inventory -n 100 --no-pager`，再核对 `.env`、虚拟环境、数据库目录权限。5000 端口只绑定回环地址，不在云服务器安全组或防火墙中开放。

## 5. Nginx、HTTPS 与访问限制

为域名设置 DNS A/AAAA 记录，云安全组只开放 SSH、80、443。先创建仅供证书验证的站点；此时**不要上传真实单据**。将以下配置写入 `/etc/nginx/sites-available/inventory`，替换域名：

```nginx
server {
    listen 80;
    server_name inventory.example.com;
    location ^~ /.well-known/acme-challenge/ {
        root /var/www/certbot;
    }
    location / { return 503; }
}
```

```bash
sudo mkdir -p /var/www/certbot
sudo ln -s /etc/nginx/sites-available/inventory /etc/nginx/sites-enabled/inventory
sudo nginx -t
sudo systemctl reload nginx
```

按 [Certbot 的 Nginx/Linux 指南](https://certbot.eff.org/instructions?os=snap&ws=nginx)安装 Certbot，然后用 Webroot 取得证书并验证续期：

```bash
sudo certbot certonly --webroot -w /var/www/certbot -d inventory.example.com
sudo certbot renew --dry-run
```

创建演示账号；命令会交互式询问密码，不要把密码写在命令行里。以后增加账号时省略 `-c`，否则会覆盖原文件。[Nginx Basic Auth 文档](https://nginx.org/en/docs/http/ngx_http_auth_basic_module.html)

```bash
sudo htpasswd -c /etc/nginx/inventory.htpasswd mentor
sudo chown root:www-data /etc/nginx/inventory.htpasswd
sudo chmod 640 /etc/nginx/inventory.htpasswd
```

把 `/etc/nginx/sites-available/inventory` 改为最终配置：

```nginx
server {
    listen 80;
    server_name inventory.example.com;
    location ^~ /.well-known/acme-challenge/ {
        root /var/www/certbot;
    }
    location / { return 301 https://$host$request_uri; }
}

server {
    listen 443 ssl;
    server_name inventory.example.com;
    ssl_certificate /etc/letsencrypt/live/inventory.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/inventory.example.com/privkey.pem;

    auth_basic "Inventory demo";
    auth_basic_user_file /etc/nginx/inventory.htpasswd;

    root /srv/inventory/frontend/dist/build/h5;
    index index.html;
    client_max_body_size 80m;

    location /api/ {
        proxy_pass http://127.0.0.1:5000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 120s;
    }

    location = /health {
        proxy_pass http://127.0.0.1:5000;
        proxy_set_header Host $host;
    }

    location / {
        try_files $uri $uri/ /index.html;
    }
}
```

`proxy_pass` 后不带 `/`，这样 `/api/...` 会原样传给 Flask。[Nginx 反向代理文档](https://nginx.org/en/docs/http/ngx_http_proxy_module.html) 默认请求体限制仅 1 MB，这里提高到 80 MB；Flask 仍按 `UPLOAD_MAX_BYTES` 校验单文件大小。[Nginx 请求体限制](https://nginx.org/en/docs/http/ngx_http_core_module.html)

```bash
sudo nginx -t
sudo systemctl reload nginx
curl -I https://inventory.example.com/
curl -u mentor https://inventory.example.com/health
```

第一条访问应要求登录（401），第二条输入密码后应返回 `{"status":"ok", ...}`。浏览器再检查首页、单据库、上传后进入审核页、食材页价格标签。若看到 413，核对 Nginx 与后端上传限制；若看到 502，检查 `inventory.service`。证书和访问控制生效后再上传真实原件。

## 6. 更新与备份

数据库、上传原件都在 `backend/var/`，更新前先停止服务并整体备份该目录。备份文件也要限制访问，不要提交到 GitHub。

```bash
sudo systemctl stop inventory
sudo install -d -m 700 -o "$USER" -g "$(id -gn)" /srv/inventory-backups
tar -czf "/srv/inventory-backups/inventory-$(date +%F-%H%M).tgz" -C /srv/inventory/backend var

cd /srv/inventory
git pull --ff-only
cd backend
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m alembic upgrade head
cd ../frontend
npm ci
npm run build:h5
sudo systemctl start inventory
```

若部署依赖发生变化，也要更新 Gunicorn 等单独安装的运行依赖。更新后检查 `systemctl status inventory`、`journalctl -u inventory -n 100 --no-pager`、`/health` 和实际页面。恢复时须先停服务，再把同一次备份中的 SQLite 数据库和上传原件**一起**恢复，之后启动服务。

## 当前限制

- 这是单机 SQLite 方案：不要用多个 Gunicorn 进程、多个应用实例或独立识别进程并行抢同一任务。需要横向扩容时先修改任务领取与数据库方案。
- Basic Auth 保护的是整个演示站点，不提供应用内用户、角色、审计与数据隔离。多人共用时会看到同一批单据。
- `OCR_PROVIDER=fake` 只供演示。真实 OCR 会调用外部服务并可能产生费用；上传原件的数据使用范围需与导师确认。
- 服务器上的 `backend/.env`、`backend/var/` 不在 Git 仓库里。导师仅 clone 源码后，需要在自己的机器上另行配置并迁移数据库。
