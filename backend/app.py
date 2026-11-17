"""启动入口。

只做三件事：读取 backend/.env、解析配置、创建应用。装配逻辑在 src/factory.py，
路由在 src/api/，业务规则在 src/services/。本文件不定义路由，也不放业务代码。

本机启动：

    python app.py

默认只监听 127.0.0.1（见 .env.example 的 HOST / PORT）。当前阶段是本地演示，
没有身份认证与访问控制，不要绑定到公网地址。

**提取 worker**（步骤 08）：`OCR_WORKER_MODE=thread` 且配了真实识别器时，
`python app.py` 会顺带起一个应用内 worker，于是单机演示不用开第二个终端。
要把它单独跑在另一个进程里（或另一台机器上）就用：

    python -m src.worker

⚠️ 起 worker 刻意**不放在 `create_app()` 里**：创建应用不该产生副作用
（见 src/factory.py 的组装规则），否则每个测试用例都会悄悄起几个后台线程。
"""

from __future__ import annotations

from dotenv import load_dotenv

from src.config import BASE_DIR, Config
from src.factory import configure_log_output, create_app
from src.worker import start_in_process

# 文件缺失时静默跳过；已存在的进程环境变量优先，.env 不覆盖它们。
load_dotenv(BASE_DIR / ".env")

config = Config.from_env()
configure_log_output(config)
app = create_app(config)
worker = start_in_process(app, config)


if __name__ == "__main__":
    app.run(
        host=config.host,
        port=config.port,
        debug=config.is_development,
        # ⚠️ 带 worker 时关掉自动重载：reloader 会起第二个进程，于是**两个进程
        # 各有一套 worker 线程**，而 SQLite 下取任务的互斥锁只在进程内有效——
        # 表现就是同一页图被调两遍，花两份钱，还不报错。
        use_reloader=config.is_development and worker is None,
    )
