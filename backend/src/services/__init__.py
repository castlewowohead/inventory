"""业务层：组织流程、执行业务规则、确定事务边界。

事务边界约定（步骤 02 建立，自步骤 03 起的 Feature 遵循）：

    class PurchaseService:
        def __init__(self, database: Database) -> None:
            self._database = database

        def confirm_line(self, ...) -> ...:
            with self._database.unit_of_work() as session:
                repository = PurchaseRepository(session)
                ...                      # 本次操作的多次写入共同提交或共同回滚

Repository 只接收已有 Session，自己不提交、不回滚、不创建 Session；
Service 之间互相调用时，把 Session 传给 `unit_of_work(session)` 加入外层事务，
不提前提交半个业务操作。详见 src/db.py。

ingredients.py 在步骤 03，采购记录在步骤 04，invoices.py 在步骤 05，
units.py 与 prices.py 在步骤 09。
"""
