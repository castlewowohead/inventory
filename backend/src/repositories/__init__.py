"""数据访问层：封装数据库读写，不包含业务判断。

约定：仓储接收 Service 传入的 Session，只做查询与持久化；提交与回滚由 Service 的
事务边界决定（见 src/db.py 与 src/services/__init__.py）。

具体仓储随对应 Feature 步骤加入：食材 03，采购 04，发票与行项目 05；
混合上传的整理批次在多文件计划 M02 的 intake.py。
"""
