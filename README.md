# Inventory

餐厅采购单据、食材与价格管理演示项目。`backend/` 是 Flask API，`frontend/` 是 uni-app H5 页面。上传单据后逐行核对入账，食材和采购价格在同一工作台查看。

## 本地运行

需要 Python 3.13 和 Node.js 24。分别在两个终端执行：

```bash
cd backend
python3.13 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp .env.example .env
.venv/bin/python -m alembic upgrade head
.venv/bin/python app.py
```

```bash
cd frontend
npm ci
npm run dev:h5
```

浏览器访问 `http://127.0.0.1:5173/`。Windows 的虚拟环境命令和其他细节见 [后端 README](backend/README.md)、[前端 README](frontend/README.md)。默认 `OCR_PROVIDER=fake` 用演示识别数据；真实识别需要在本机 `backend/.env` 单独配置凭据。

## 文档

- [结构说明](backend/docs/struct.md)
- [服务器部署](backend/docs/deploy.md)

本仓库不包含本机 `.env`、数据库、上传原件和真实单据样本。
