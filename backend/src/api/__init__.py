"""表示层：处理 HTTP 输入与输出，调用 Service，不直接操作数据库。

本层负责把请求转成参数、把结果或异常转成响应；业务判断留在 Service。
业务接口随对应 Feature 步骤加入（食材 03、采购记录 04、发票 05、价格统计 09）；
未实现的能力不在此注册返回假成功的路由。
混合上传的整理批次在 intake.py（多文件计划 M03）。
"""

from __future__ import annotations

from flask import Flask


def register_blueprints(app: Flask) -> None:
    """注册所有 Blueprint。新增接口在此登记，不在应用工厂里散落路由。"""
    from src.api.health import health_bp
    from src.api.ingredients import ingredients_bp
    from src.api.intake import intake_bp, source_memory_bp
    from src.api.invoices import invoices_bp
    from src.api.prices import prices_bp
    from src.api.purchases import purchases_bp
    from src.api.spreadsheets import spreadsheets_bp

    app.register_blueprint(health_bp)
    app.register_blueprint(ingredients_bp)
    app.register_blueprint(purchases_bp)
    app.register_blueprint(invoices_bp)
    app.register_blueprint(spreadsheets_bp)
    app.register_blueprint(prices_bp)
    app.register_blueprint(intake_bp)
    # 商家记忆跨批次复用，所以它有自己的前缀（多文件计划 M07）。
    app.register_blueprint(source_memory_bp)
