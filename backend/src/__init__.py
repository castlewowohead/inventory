"""后端应用包。

分层约定见 docs/struct.md：API → Service → Repository，外部调用经 integrations。

已落地（步骤 01）：
- config.py   配置加载与校验
- errors.py   业务异常与统一错误响应
- factory.py  应用工厂与依赖组装

已落地（步骤 02）：
- db.py        Engine、Session 与由 Service 控制的事务边界
- models.py    ORM 声明基类与命名约定（业务表随 Feature 步骤加入）
- contracts.py 与厂商无关的识别请求/结果契约
- integrations/file_storage.py  本地原始文件存储
- integrations/ocr.py           识别器接口与明确标识的 Fake 实现
- migrations/   Alembic 迁移环境与基线修订（位于 backend/ 下，不在本包内）

待落地：业务模块随步骤 03 起的 Feature 加入（食材 03、采购与换算 04、发票 05）。
"""

__version__ = "0.1.0"
