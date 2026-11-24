# Inventory 后端

餐厅食材与采购价格管理 Demo 的后端。本目录是一个独立工程：依赖声明、配置示例、迁移、测试与启动说明全部随本目录交付。

需求范围见 [docs/imp_notes.md](docs/imp_notes.md)，分层约定见 [docs/struct.md](docs/struct.md)，实施顺序与 Check List 见 [docs/stru_dev.md](docs/stru_dev.md)。

## 1. 当前进度

已完成 **步骤 00（工程基线）**、**步骤 01（应用入口与请求处理）** 与 **步骤 02（基础设施与测试骨架）**，即 **里程碑 A：后端骨架完成**——独立安装、启动、迁移、测试四条通路可用；在此之上完成 **步骤 03（食材数据库）**。

数据库仍按基线使用 **SQLite 文件库**（`var/inventory.db`）：2026-11-10 确认不改用 MySQL／PostgreSQL，服务器数据库留到步骤 11 的共享部署再决定。引擎相关的处理都集中在 [src/db.py](src/db.py) 的 `is_sqlite` 分支，换引擎时主要是改 `DATABASE_URL` 与该文件。

**当前优先链条是 Feature 4。** 2026-11-10 收到 Mentor 反馈「Feature 4（发票录入）是老板每天最常用的场景」，因此 [docs/stru_dev.md](docs/stru_dev.md) 的步骤 04—08 成为优先链条：步骤 04 削薄为「发票确认行的写入目标」（采购记录 + 精确数值 + 手工录入），单位换算与价格统计移到步骤 09。真实 OCR 的账号与凭据因此进入优先链条，需要尽早准备；步骤 04—06 用手工与表格入口即可完成，不被凭据阻塞。

**步骤 03（食材数据库、基础搜索与快速新增）已完成并验收**（2026-11-10，Check List 03.0—03.5 已勾选）：分类与标准食材的表、迁移与接口均可用。按 2026-11-10 的确认，本轮**不建独立别名表与供应商表**：一条食材只有一个英文名 + 一个中文名 + 可选规格，供应商的原始商品描述由步骤 04 的采购记录承担（详见 [docs/stru_dev.md](docs/stru_dev.md) 步骤 03）。

**步骤 04（采购记录、精确数值与手工录入）已完成并验收**（2026-11-10，Check List 04.0—04.9 已勾选）：`purchases` 与 `purchase_revisions` 两张表、迁移与六个接口均可用。金额与数量的**精确存储已解决**——按整数最小单位存，进出都是精确整数运算，不经 `float`（见第 10 节）。这一轮定下的四条口径：**行金额是权威**（差额超 $0.05 要显式确认）、**采购日期可空**（识别不出允许留空，代价是进不了月度统计，要靠 `undated=true` 筛出待补清单）、**纠错是直接修改 + 修改日志**（不强制作废重录）、**退货/折让（负数）行本轮不支持**（0 元赠品行可以录）。

**2026-11-10 追加：退货 / 折让等调整行。** `line_kind="adjustment"` 的行金额与数量可以为负，食材可以不填（整单折让）。这类行留着**对账**，但**默认不出现在查询结果里**、也不参与步骤 09 的价格统计——把一笔退货混进加权均价只会让价格悄悄偏低。仍然不做库存调整、退款管理与完整退货流程。（上一段「退货 / 折让本轮不支持」说的是步骤 04 刚完成时的状态，已被这一段取代。）

**步骤 05（原始发票、审核草稿与逐行确认）已完成并验收**（2026-11-10，Check List 05.0—05.15 已勾选）：`invoices` / `invoice_files` / `invoice_lines` 三张表、`purchases.invoice_line_id` 关联列与 15 个接口均可用。这一步把 Feature 4 的主干跑通了：**上传照片或 PDF → 生成草稿 → 改值 / 匹配食材 / 跳过 → 逐行确认 → 进采购历史 → 回得到原始单据**。几条口径值得单独记住：

- **系统从不自己把数据写进采购历史**，唯一入口是老板对某一行点确认；确认走的是步骤 04 的同一套校验与写入（`entry_source="invoice"`）。
- **认不出来不等于 0**：行上的数值可空、接口输出 `null`；缺必填值的行不能确认，报错里列出缺哪几个字段。
- **逐行独立**：批量确认里一行失败不回滚其它行，HTTP 仍是 200，每行各自带结果。
- **原始识别值永不改动**（`raw_fields` 一列 JSON，含原文、状态、页码与区域），老板改过的值另存在行上的业务列。
- **匹配只给候选**：「这句描述以前确认到哪个食材」直接查采购历史，同来源优先，最多 5 个，**不自动确认**。
- **收口确认**：审完所有行要点一次「完成」，接口把「已确认 N 行、跳过 M 行、几个文件、对账差多少」摆出来。这是**漏行唯一会被拦住的时刻**——各行之和对不上总额只能抓到一部分，手写单常常连总额都没有。

**步骤 06（CSV / Excel 导入导出）已完成并验收**（2026-11-10，Check List 06.0—06.6 已勾选）：`POST /api/imports`（建批次）、`POST /api/imports/preview`（只读预览）、`GET /api/imports/template`（中文表头模板）与 `POST /api/invoices/export`（勾选已完成单据后导出明细）均可用。表格走的是**同一套审核流程**：导入建出 `entry_source='import'` 的草稿批次，修改、匹配食材、逐行确认、撤销、收口全部用步骤 05 的同一组接口，入账走步骤 04 的 `create_purchase`。几条值得记住的：

- **不猜**：`1,234.5` 的逗号含义不明、`03/04/2026` 的日月顺序不明时**直接报错并定位到单元格**，要老板显式选 `number_format` / `date_order`。猜错一次就是一个错价格永久留在历史里。
- **不经 float**：XLSX 直接读 OOXML 的 `<v>` 文本再交给步骤 04 的 `parse_decimal`，不走 openpyxl 的常规 reader（那条路会先把十进制单元格转成 `float`）。
- **Excel 是输入格式，不是编辑器**：改数据要在程序里改；改完文件重传会建**新批次**而不是更新旧记录，这一点在导入响应的 `notice` 里明说。
- **重传不重复入账**：同一个文件的 `sha256` 撞唯一约束 → 409 并指回原批次。
- **导出是明细不是总额**：21 列（食材、原始描述、数量、单位、单价、行金额、币种、入口、来源单据与行号……），默认只导未作废的采购行，所以**待确认的草稿行不会混进价格明细**。

**步骤 07（真实单据与 OCR 候选评估）已完成**（2026-11-10）：这一步**不写运行时代码**，产出是一份可复查的评测与选型记录，细节见 [docs/impl_record.md 步骤 07](docs/impl_record.md)。

- **选中 `qwen3.5-ocr` 为主、`qwen3-vl-plus` 作第二意见**，生产并行跑这 2 个；`qwen-vl-ocr` 落选。依据是 6 张印刷体供应商发票与人工 Ground Truth 的逐字段比对（96.6% / 92.0% / 48.9%）。
- **`vanchin/deepseek-ocr` 标记为阻塞**：型号列表里有，78 次调用全部返回「产品未开通」。失败记录保留为证据。
- **手写件的结论是一个流程决定，不是模型排名**：中文商品名跨模型分歧率 **96.0%**、数字 **66.9%**，所以步骤 08 的「手写件默认不预填」维持不变，商品名那一格也确认**不走 OCR**（裁图给老板看 + 从食材库选）。
- ⚠️ **本步骤最重要的发现不是「模型会捏造」，而是「模型会把列读错位」**：抓到的 6 次数值编造全部是把小票的税率代码列当成了数量列，报出 `quantity: 0` / `quantity: 3`。这类错误**算术校验拦不住**，看起来还完全合理，却会让采购记录的价格彻底失真。
- ⚠️ **墨迹检测只做到一半**：表格行结构（行容量）可靠，逐行判定「写了字 / 空的」没做到——空行与写字行的像素密度在三种基准下全部重叠。全自动逐格判定移交步骤 08，且**必须先做配准**，三条试过并失败的路留在附录里。（步骤 08 批次 2 用**页内**逐像素配准把判空做出来了，但它**仍然不能用来丢弃**，见下。）

**评测工具在 [tools/](tools/)，不是运行时代码**：不被 `src/` 导入、不参与 `pytest`。步骤 08 之后 Pillow 与 pypdf **已随运行时代码移入 `requirements.txt`**（墨迹/格线检测与数字 PDF 文字层进了 `src/`），所以 `requirements-tools.txt` 现在只引用运行依赖。

**步骤 08（接入真实 OCR）两个批次都已完成**（2026-11-11）：批次 1 是整条「真实 OCR → 审核」通路（适配器、三条分流路径、分层校验、后台 worker、草稿写入）；批次 2 是**「行带 ↔ 商品行」锚定 + 墨迹判空 + 裁剪自动落位**。细节见 [docs/impl_record.md 步骤 08](docs/impl_record.md)。⚠️ 两项验收标准（08.3a、08.3d）**仍然留空没有打勾**，原因写在下面。

- **三条路径按耗时分流**：数字 PDF 直接抽文字层（**根本不调 OCR**，零成本）；印刷体扫描件 1 轮内容识别；手写件 2 轮（先问结构、再读内容）。分流依据**与识别出的内容无关**——按供应商路由为主，本地笔画判别兜底。
- **多模型交叉裁决**：`qwen3.5-ocr` + `qwen3-vl-plus` 并行跑，**一致才预填并标注，分歧一律置 `UNREADABLE` 必须人填**。全量实跑中它真的抓到了步骤 07 记录的那一格——`5 85` 被两个模型读成 `5.85` 与 `58`。
- **手写件一律不预填**：68 张手写件 + 4 张小票上实测 **0 处预填泄漏**。模型在这里的职责只剩定位格子、裁剪原图、排顺序；两个模型读到的原文仍然保留在 `raw_fields` 里供人对照。
- **行容量是硬上限**：模型报的行数超过表格印着的行格数，多出来的一律丢弃。79 份样本上 **0 处超限**。
- **规范化显式可见**：全角转半角、去千分位、剥货币符号每一步都记进 `review_metadata.normalization`，`raw_text` 原样不动；**定不死就拒绝**（`1,234` 在两种约定下差 1000 倍，宁可留空让人填）。
- **批次 2：行带与商品行的对应关系建立起来了**——靠读**最左列印着的行号**（那是印刷数字，模型最准的那一类），两个模型各读一遍再过四关本地校验（长度 / 连号 / 连续 / 两模型一致）。锚定成立的页**裁剪图能自动摆到对应输入框旁边**，不用再显式指定行带列带序号。⚠️ **只有纸上印着行号的单据能锚定**：FengYuan 那一家没印，18 页一页也成立不了；印着行号的两家良率约 80%，不成立的页退回批次 1 行为。**误锚 0 处**——不确定的一律落到「未建立」。
- ⚠️ **批次 2 顺带修掉了批次 1 的一个静默错误**：表格竖线检测把每条线记成了它**关于页面中线的镜像**位置，所以 `crop(row, column)` 一直裁的是镜像那一列——行对、列错，而且不报错。同一个镜像在内容区边界计算里也有一份，都修了并加了用例守着。
- ⚠️ **墨迹闸门做出来了，但「丢弃」那一半没有达成**：页内逐像素配准能判出哪些行是空的（人工核对子集 3 页逐行判对），但全量实跑抓到 TangFa p14 第 3 行纸上写着 `4 x30 / 8.99 / 1079.00`、铅笔太淡被判成空行。拿它当行数上限就会**悄悄删掉一条真实采购行**——那和填进一个错价格是同一类错误。所以闸门只作证据、并支撑上面那条行↔商品行的对应，**硬上限仍然只有行容量**。08.3a 因此继续留空。

**全量端到端验收**（2026-11-11，79 份真实样本、真调模型，两个批次各跑一轮）：

| | 批次 1 | 批次 2 |
| --- | --- | --- |
| 结果 | 79/79 成功，9.7 分钟 | 79/79 成功，5.4 分钟 |
| 分流 | 72 手写 + 6 印刷 + 1 数字 PDF，全对 | 同左 |
| 手写件预填泄漏 | 0 | 0 |
| 超出行容量 | 0 | 0 |
| 草稿行 | 307 | 295 |
| 行号锚定 / 行↔商品行对应 | — | 28 页 / **18 页** |
| 报告 | `samples/real/eval/e2e_step08.json` | `samples/real/eval/e2e_step08_batch2.json` |

⚠️ **批次 2 真正能把裁剪图自动落位的是 18/79 页**，其余的页老板仍要自己在原件上数行。
`pytest` 916 项全绿，`tools/eval_adapter.py replay` 六项全过（印刷件预填精度 97.6%、编造 0，与批次 1 逐字相同）。

**步骤 09（单位换算、月度价格、来源比较与涨价提醒）已完成**（2026-11-13，Check List 等用户确认后勾选）：`pack_specs`（箱规）与 `price_alert_settings`（阈值与开关）两张表、11 个 `/api/prices` 接口均可用。细节见 [docs/impl_record.md 步骤 09](docs/impl_record.md)。这一步把 Feature 2 的后半截补上了：**原始计价单位的价格现在能换算成「每个基础单位多少钱」，并按月、按来源汇总，涨得超过阈值会被挑出来。** 几条口径值得单独记住：

- ⚠️ **标准化价格不落列，查询时算。** `purchases` 一列都没加——箱规一改、采购记录一改，价格立刻跟着对；落一列回填就会出现「列里存着旧值，而它看起来仍然像个可信的数字」。代价是统计要把窗口内的记录取回内存算，而**触顶（2 万条）时报错，不截断**：少了尾巴的均价看起来完全正常。
- ⚠️ **箱规原文不能自动解析。** 真实单据上 `1000g x 10` 是「每箱 10×1000g」，而 `6X30` 是「**6 箱** × 每箱 30 磅」——同形不同义、差 6 倍。所以只有原文里恰好出现**一个计量单位**时才给候选，裸数字一律不给；也**不许**拿「乘出来刚好等于这一行的数量」当判据（`6X30 = 180` 恰好等于那一行的数量，而那个读法是错的）。
- **箱规维护一次、历史与以后都生效**：键是（食材, 来源, 包装单位），查找先精确来源、再不限来源。代价是**改箱规会让历史均价跟着变**（与步骤 04「统计按当前值算」同一口径），所以保存时返回「这条箱规影响 N 条采购记录」。
- **算不出来是 `null`，不是 0**：0 是一个具体的价格（赠品行就是 0 元）。四种状态 `available` / `pending_pack_spec` / `unit_unknown` / `incompatible_unit`，每个统计响应都带「有几条没算进来、分别为什么」。
- **加权均价 = 总金额 ÷ 换算后的总数量**，不是把各行单价平均（差 7%）。标准化单价保留 **4 位小数**——真实数据里有 `$0.0052/g`，2 位会舍成 `$0.01/g`，贵 92%。
- **本月与上月比同样的天数**；缺基期**不显示 0% 变化**。**不同币种不相加**（本轮没有汇率换算），加总类接口多币种时报 400 并列出可选币种。
- **调整行（退货/折让/运费）永不进均价**，所以标准化价格是**商品行价格、不含运费与整单折让**——这句话随每个统计响应一起返回。
- **体积与重量永不互转**（没有密度）；`cup` / `can` 这类容器名词归**包装单位**而不是体积单位：「48 cups 酸奶」指 48 个杯装，不是 48 杯容积。

**步骤 09 的真实数据实跑**（2026-11-13，`tools/e2e_prices.py`，报告 `samples/real/eval/e2e_step09.json`）：43 行真实采购**全部算出标准化价格**，**0 次模型调用**（数字 PDF 走文字层）、2.5 秒。其中 5 页数字 PDF 走的是**真实提取链路**（上传 → 文字层 → 草稿 → 逐行确认 → 采购记录 → 价格），29 行全部入账；6 张印刷件用的是步骤 07 的**人工 Ground Truth 值**（不是本次模型读的，报告里标明）。实跑顺带抓到并修掉三个真问题，最要紧的一个是：**尾部单位表只有 lb / kg / g / each 几个写法，于是真实单据上 `72 heads`、`16 gal.`、`48 cans` 这 7 行的数量被静默判成"认不出"，价格一条也算不出**。

⚠️ **步骤 09 有三处真实样本验不到的东西**（别把它们说成已验证）：真实单据里**没有一条食材是从两个来源买的**，所以**来源比价只有 pytest 覆盖**；真实采购全部发生在 2026 年 6 月，所以**月度环比也只有 pytest 覆盖**；4 张零售小票共 **34 行录不进来**——纸面上没有计量单位，老板必须自己说那是「一件」还是「一斤」。**真调模型的那一轮已经跑了**（2026-11-13，`--with-ocr`，12 次调用、36 秒）：6 张印刷件 14 行逐行与人核过的 GT 比——**每单位价格相同 12 行、不同 0 行**，其中 11 行真的入账并算出标准化价格；另 2 行被交叉裁决判为「需人填」（纸面那两行各写了两个分重量，数量本身有歧义），1 行三个数字全对但**缺单位、确认被正确拒绝**——没有单位就没有价格，猜一个单位会让价格差一个数量级。

**步骤 10（搜索增强、最近/常用、食材合并）后端已完成**（2026-11-14，Check List
等用户确认后勾选）：`food_item_merges` 一张表、`food_items.needs_review` 一列、
`Uncategorized` 分类 seed、三个新 service 模块与三个新接口均可用。细节见
[docs/impl_record.md 步骤 10](docs/impl_record.md)。这一步补上的是 Feature 3 的剩余
能力与 Feature 4 的「纠正记忆」：**打几个字能搜到（容忍缩写与拼写差异）、重复食材
能合并掉、印刷单据上的食材能自动认出来或建好、收口时能看见这张单按分类花了多少钱。**
几条口径值得单独记住：

- ⚠️ **搜索是两个接口，不是一个接口加开关。** `GET /api/food-items` 的语义**一个字
  没改**（食材库那一页：找确定的某一条然后改它或删它，所以搜 `CNTRY CROCK` 返回
  0 条是正确行为）；模糊匹配在新的 `GET /api/food-items/suggest`。
- ⚠️ **分数再高也不替人选中。** 候选列表的入选线是 0.70，但「零点击选中」走的是
  **精确命中**（规范化后完全相同），不吃模糊分数——`pork` 与 `park` 的比值是 0.75，
  当候选没问题，拿来自动选中就是把一条采购记录静默挂到错误的食材上。
- **关键词认食材的顺序是「已确认的描述映射 → 关键词精确命中 → 自动建」。**
  ⚠️ 顺序不能反：关键词排前面会把老板亲手确认过的映射覆盖掉，而界面上看起来像是
  他自己选的（`imp_notes.md` 4.8 写的也是「优先复用已确认的映射」）。
- ⚠️ **绝不拿 `each` 兜底。** 单位归一不出来就**不建食材**，改成把新建表单预填好、
  让老板补那一格。真实样本里 35/48 行（73%）的单位要么是包装单位、要么纸面根本
  没印——给它们随便填一个 `each`，和「算不出来时价格写 0」是同一类错误。
- **分类是副产品，不是输入**：由本地词表从关键词头词猜，认不出落「未分类」，
  ⚠️ **绝不按词表去新建分类**（真实样本算过：NewMoys 一家 13 行会造出 7 个类别，
  而那 7 个全属肉类）。分类只是默认值，下拉里照样能改；它挂在食材上而不是采购
  记录行上，所以**改一次全部历史自动跟着走**。
- ⚠️ **合并不可逆**，这句话固定出现在每次预览里。矛盾的箱规与不同量纲的基础单位
  **拒绝合并而不是替老板挑一个**；被拦住时什么都没动，不做"能并多少并多少"。
- **收口面板按分类摊开本单金额**，`adjustment`（运费 / 折让）**单列一桶、不混进
  任何分类**（与步骤 09 的价格统计同一条口径）；多币种时整张面板不出。

**步骤 10 的真实数据实跑**（2026-11-14，`tools/e2e_keywords.py --all-handwritten`，
报告 `samples/real/eval/e2e_step10.json`）：**78 份真实样本、真调模型、4.6 分钟、
78/78 成功、17/17 条判定通过**。NewMoys 5 页 **13 行 → 7 条食材**、分类全部落
「肉类」、单位全部从 `lbs.` 归一成 `lb` 自动建成并标「待确认」、**收口面板每页
1 格**（13 行真的压成了一格）；Kunkee 的「箱」与 4 张小票**自动建 0 条**；
68 张手写件 **207 行里 0 行**带关键词痕迹、**0 行**被选上食材。

⚠️ **步骤 10 有两处边界**（别说成已验证）：**4 张小票被本地判别器判成手写件**
（热敏小票笔画不均匀，步骤 08 量过的阈值本来就是「小票 0/4 判成印刷」），所以
它们走不到关键词路径——结果（不自动建）是对的，但**原因与 10.C 原文设想的不一样**；
**「单位归一不出来就不建」只有 Kunkee 一行真实样本验得到**，而且两轮实跑表现不同
（取决于两个模型要不要在那一行上读成同一串），主要覆盖来自 pytest。
⚠️ **前端的 10.14 / 10.17（`frontend_plan.md` F14「先建后选」）不在本轮**
（2026-11-14 与用户确认），所以现在**不能说「选食材那一屏已经改好了」**：
后端认得出、建得出，界面怎么用要等前端那一轮。

**当前发票审核**：手工录入、发票逐行确认与 CSV / Excel 表格导入都能形成采购记录；真实照片与多页 PDF 可进入 OCR 审核流程，手写件不预填，审核人员对照整页原件填写。后端保留识别原文、行号与墨迹证据，但不再提供格子裁图接口。墨迹判空仍只作证据，不会自动丢弃采购行。

| 能力 | 状态 | 落地步骤 |
| --- | --- | --- |
| 虚拟环境、依赖声明、配置示例、忽略规则、本文档 | 可用 | 00 |
| 薄入口 `app.py`、应用工厂 `create_app()`、配置加载与校验 | 可用 | 01 |
| 统一异常到 HTTP 响应的映射、基本日志、`GET /health` | 可用 | 01 |
| SQLAlchemy 会话与由 Service 控制的事务边界 | 可用 | 02 |
| Alembic 迁移环境与基线修订 | 可用 | 02 |
| 分类与标准食材的表、迁移与 7 个初始分类 | 可用 | 03 |
| 食材/分类接口：快速新增、编辑、删除、按中英文与规格搜索、分类筛选 | 可用 | 03 |
| 本地原始文件存储（往返读写与路径边界） | 可用 | 02 |
| 识别契约（原文、页码、区域证据）与明确标识的 Fake 识别器 | 可用 | 02 |
| 可注入依赖的 pytest 基础 | 可用 | 02 |
| 采购记录的表、迁移与精确数值存储（整数最小单位，不经 float） | 可用 | 04 |
| 采购接口：手工录入、按食材/来源/日期查询、直接修改 + 修改日志、作废 | 可用 | 04 |
| 退货 / 折让 / 其它调整行（负数金额，可不指向食材） | 可用 | 04 |
| 库存调整、退款管理、完整退货流程（审批 / 关联原单 / 退款状态） | **不做** | 不在范围 |
| 发票上传、原始文件保留与草稿（含手工加行） | 可用 | 05 |
| 逐行确认：改值、匹配食材、跳过、撤销确认、确认时当场新建食材 | 可用 | 05 |
| 行匹配：历史原始描述精确命中并给候选（不自动确认） | 可用 | 05 |
| 整单对账与收口确认（已确认 N 行、跳过 M 行、差额提示） | 可用 | 05 |
| 重复上传判定（同一文件硬拦、来源 + 单号 + 总额软提示） | 可用 | 05 |
| 置信度分级、区域高亮等审核界面交互 | **前端任务** | 不在本后端范围 |
| CSV / Excel 导入：预览、列映射、建待审核批次（不经 OCR） | 可用 | 06 |
| 导入模板下载（中文表头，CSV / XLSX） | 可用 | 06 |
| 采购明细导出（CSV / XLSX，可筛选，不只导总额） | 可用 | 06 |
| 真实 OCR 适配器（qwen3.5-ocr + qwen3-vl-plus 并行交叉） | 可用 | 08 |
| 单位换算（质量/体积/计数，美制体积） | 可用 | 09 |
| 包装规格（1 箱 = 多少基础单位）与待补箱规清单 | 可用 | 09 |
| 标准化单价、月度加权均价、本期/基期比较 | 可用 | 09 |
| 采购来源比价（同一食材、同一基础单位、同一币种） | 可用 | 09 |
| 涨价提醒（阈值可调、可关闭） | 可用 | 09 |
| 汇率换算、按生效日期分段的箱规、按食材的提醒阈值 | **不做** | 不在本轮范围 |
| 模糊搜索、最近/常用、重复食材合并 | 未实现 | 10 |
| 混合上传的整理批次：建/恢复批次、**逐文件**上传、处理状态、回看原件 | 可用 | M03 |
| 图片按张、PDF 按页切成可归类内容（**多页 PDF 不等于一张发票**） | 可用 | M03 |
| CSV / XLSX 按行进入整理批次（列映射、重复提示） | 可用 | M04 |
| 整理阶段的人工归组、批量移动与撤销 | 可用（后端 API） | M06 |
| 整理阶段的日期/商家候选（数字 PDF 走文字层零成本；日/月顺序定不死时按默认的月/日/年读） | 可用 | M05 + 第 15 节 |
| 确认商家后用保存的原文重解析日期（**不重新识别**） | 可用 | M05 |
| 已整理内容幂等送审、按单据拆并页、回看原件及入账后分类修订 | 可用（后端 API） | M08 |
| 商家记忆与相似堆：同文件参考、历史写法/别名、版式相似建议（**一次模型都不调**） | 可用（后端 API） | M07 |
| 可更正的来源别名映射（`COSTCO` / `Costco` 不分成两组） | 可用 | M07 |
| 整理内容的送审状态在 `/items` 上透出（`handed_off_at`） | 可用 | M12 |
| 混合上传 → 整理 → 送审 → 填写 → 确认 → 价格历史的整条链路 | 可用（Fake 与**真实识别器**下都已在浏览器里实测） | M12 |

数据库里目前有十一张业务表（分类、标准食材、采购记录与其修改日志、单据、单据文件、行项目、提取任务、供应商提取路径、箱规、涨价提醒设置，加 Alembic 的版本表），加上多文件整理批次自己的那一组 `intake_*`（M02 五张、M06 两张、M08 一张、M07 一张来源别名）。⚠️ **M07 的 `intake_source_aliases` 不是供应商表**：它只有「纸面上这种写法 → 人确认过的哪一家」一个映射，而「这一家现在叫什么」「这个版式属于哪一家」都是派生值，没有表。⚠️ **步骤 09 没有给 `purchases` 加标准化价格列**：那是查询时算的派生值。识别结果可以来自真实 OCR，也可以来自明确标注的假数据——`recognition.is_fake` 一路传到接口响应，两者永远分得清。未实现的业务不注册路由，也不提供返回假成功的接口：访问这类路径得到的是 404，而不是空结果或假数据。本文档中标注「未实现」的能力在对应步骤完成后再更新。

## 2. 独立性约定

- 本目录不依赖、不导入、不读取、不软链接同级的 `kitchen-ledger/` 与 `restaurant-erp/`，也不依赖它们的站点、云项目、凭据与 Node/Deno 环境。两个旧项目仅供开发时查阅。
- 依赖、配置与启动脚本按本项目基线重新建立，未从旧项目复制；路由、身份验证与云函数也未移植。
- 后续步骤若从旧项目转写代码或样本，代码与样本必须实际落入本目录，并在第 16 节记录来源、修改点与对应测试；来源记录不构成运行依赖。

## 3. 目录结构

```text
backend/
├── app.py                  # 薄入口：读 .env、解析配置、创建应用
├── README.md               # 本文档
├── requirements.txt        # 运行依赖（固定版本）
├── requirements-dev.txt    # 开发与测试依赖（含 -r requirements.txt）
├── requirements-tools.txt  # 评测工具依赖；步骤 08 后 Pillow/pypdf 已移入运行依赖
├── pytest.ini              # 测试入口配置（testpaths、pythonpath）
├── alembic.ini             # 迁移配置（纯 ASCII，见第 10 节）
├── .env.example            # 配置示例，复制为 .env 后填写本机值
├── .gitignore              # 忽略规则：虚拟环境、密钥、运行期数据、真实单据
├── .venv/                  # 本机虚拟环境（忽略，不交付）
├── var/                    # 运行期数据：SQLite 文件与上传文件（内容忽略）
├── docs/                   # 需求、架构与实施计划
├── samples/
│   ├── real/               # 真实单据、Ground Truth、识别结果（含客户数据，整体忽略，不交付）
│   └── synthetic/          # 合成样本，随项目交付
│       └── text_layouts/   # 步骤 07.10：英文合成文本版式 + GT，供文字层解析回归
├── tools/                  # 评测工具。**不被 src/ 导入、不参与 pytest**（会花钱）
│   ├── split_scanned_pdf.py        # 扫描 PDF 拆单页图，结构不符就报错退出
│   ├── ocr_batch.py                # 逐页调用并落盘原始响应；凭据只从环境变量读
│   ├── run_ocr_matrix.sh           # 跑满候选型号 × 两组图，统一提示词
│   ├── gt_schema.py                # GT 结构与校验，字段名从 src/contracts.py 导入
│   ├── gt_printed_invoices.py      # 6 张印刷件 GT（开发方标注）+ 算术体检
│   ├── gt_from_xlsx.py             # 转换用户提交的 4 份小票 GT，带留痕与订正
│   ├── gt_from_legacy_fixtures.py  # 07.10：旧英文合成样本改写（只在开发时读一次旧目录）
│   ├── ink_rows.py                 # 表格行结构检测（行容量可靠，逐行判空未达成）
│   ├── eval_ocr.py                 # 步骤 07：模型本身的评测与计分
│   ├── eval_adapter.py             # 步骤 08：**适配器**的回归（replay 零成本 / live 真调）
│   ├── e2e_extraction.py           # 步骤 08：**整条链路**跑全量真实样本的端到端验收
│   ├── e2e_prices.py               # 步骤 09：真实单据能不能算成可比价格（默认 0 次模型调用）
│   └── e2e_keywords.py             # 步骤 10：关键词认食材、自动建食材与收口面板（⚠️ 真调模型）
├── migrations/             # Alembic 迁移环境
│   ├── env.py              # 连接串来源、target_metadata、SQLite 批处理模式
│   ├── script.py.mako      # 新修订模板（含编写约定）
│   └── versions/           # 修订脚本（基线 → 建表 → 初始分类）
├── src/
│   ├── config.py           # 配置加载与校验
│   ├── errors.py           # 业务异常与统一错误响应
│   ├── factory.py          # 应用工厂、依赖组装、日志配置、访问日志
│   ├── db.py               # Engine、Session、事务边界
│   ├── models.py           # ORM 声明基类与命名约定、十一张业务表、UtcDateTime、DecimalAsInteger
│   ├── contracts.py        # 与厂商无关的识别请求/结果契约
│   ├── normalization.py    # 数值与日期规范化：一份口径，表格与 OCR 两条通路共用
│   ├── worker.py           # 提取任务的后台 worker（提取不挂在 HTTP 请求里）
│   ├── documents/          # 本地文档处理：**一行代码都不访问外部服务**
│   │   ├── files.py        # 格式嗅探、HEIC 解码、EXIF 方向、data URL 媒体类型
│   │   ├── pdf.py          # 文字层 / 扫描页的区分、抽片段、取内嵌整页图
│   │   ├── textlayer.py    # 文字层还原成行列结构（⚠️ 行列归位要写代码）
│   │   ├── grid.py         # 去斜、找格线、切行带列带
│   │   └── classify.py     # 印刷 / 手写的本地判别（阈值是量出来的）
│   ├── api/                # 表示层：HTTP 输入输出
│   │   ├── health.py       # GET /health
│   │   ├── ingredients.py  # 食材与分类接口
│   │   ├── purchases.py    # 采购记录录入、查询、纠错与作废接口
│   │   ├── invoices.py     # 发票上传、草稿维护、逐行确认、对账与收口
│   │   ├── spreadsheets.py # 表格导入预览、建批次、模板与明细导出
│   │   ├── prices.py       # 标准化价格、月度与来源统计、涨价提醒、箱规维护
│   │   └── schemas.py      # 请求形状校验与响应序列化
│   ├── services/           # 业务层：流程、业务规则、事务边界
│   │   ├── ingredients.py  # 规范化、重复判定、基础单位白名单、删除守卫
│   │   ├── purchases.py    # 数值解析与精度、行金额校验、纠错与修改日志
│   │   ├── invoices.py     # 上传与判重、草稿、行状态、行匹配、逐行确认、对账
│   │   ├── spreadsheets.py # 导入批次与导出（Excel 是输入，不是编辑器）
│   │   ├── spreadsheet_parsing.py  # 列映射与保守的精确解析（不猜逗号与日月）
│   │   ├── extraction.py   # 分流、分层校验、行容量上限、草稿写入、识别证据
│   │   ├── matching.py     # 文本相似度原语 + 缩写/噪声/分类词表（纯计算，步骤 10）
│   │   ├── suggestions.py  # 候选食材：模糊匹配、最近 / 常用（步骤 10）
│   │   ├── merging.py      # 重复食材合并：预览、拦路冲突、一次事务（步骤 10）
│   │   ├── units.py        # 单位换算（纯计算，美制体积）与包装规格原文解析
│   │   ├── prices.py       # 标准化价格、加权均价、期间比较、来源比价、涨价判断
│   │   └── validation.py   # 三个服务共用的文本/取值/分页校验（rule of three）
│   ├── repositories/       # 数据访问层：数据库读写
│   │   ├── ingredients.py  # 分类与食材的查询与写入
│   │   ├── purchases.py    # 采购记录与修改日志的查询与写入
│   │   ├── invoices.py     # 单据、单据文件与行项目的查询与写入
│   │   ├── prices.py       # 箱规与涨价提醒设置的查询与写入
│   │   └── extraction.py   # 提取任务（先占后跑）与供应商路径的查询与写入
│   └── integrations/       # 外部集成
│       ├── file_storage.py # 本地原始文件存储
│       ├── spreadsheets.py # 有界的 CSV / OOXML 读写，数值不经 float
│       ├── dashscope.py    # 阿里云 Model Studio：调用、重试、输出解析、三态归类
│       ├── crosscheck.py   # 多模型逐字段裁决：一致才预填，分歧必须人填
│       └── ocr.py          # 识别器接口 + Fake 实现 + 真实适配器的装配
└── tests/
    ├── conftest.py         # 注入独立配置与临时目录依赖的夹具
    ├── test_baseline.py    # 工程基线自检（步骤 00）
    ├── test_config.py      # 配置解析与校验
    ├── test_entry.py       # 入口保持薄、导入不起服务
    ├── test_errors.py      # 异常到 HTTP 响应的映射
    ├── test_factory.py     # 配置注入、依赖组装与日志
    ├── test_health.py      # 健康检查
    ├── test_db.py          # 会话、提交/回滚、SQLite 行为
    ├── test_migrations.py  # 在临时数据库上真实执行迁移
    ├── test_file_storage.py# 文件往返读写与路径边界
    ├── test_contracts.py   # 识别契约的约束
    ├── test_ocr.py         # Fake 标识、无凭据启动、启动不连外部服务
    ├── test_ingredients_service.py  # 食材与分类的业务规则
    ├── test_ingredients_api.py      # 食材与分类的接口
    ├── test_purchases_service.py    # 采购记录的业务规则与精确数值
    ├── test_purchases_api.py        # 采购记录的接口
    ├── test_invoices_service.py     # 上传、草稿、行匹配与逐行确认
    ├── test_invoices_api.py         # 发票接口（含 multipart 上传）
    ├── test_spreadsheets.py         # 表格导入与导出
    ├── test_normalization.py        # 数值与日期规范化（两条通路共用的口径）
    ├── test_documents.py            # 本地文档处理：格式、PDF、格线、判别、文字层
    ├── test_ocr_adapter.py          # 真实适配器与交叉裁决（假 HTTP，不碰网络）
    ├── test_extraction_service.py   # 分流、不预填、行容量、重试闸门（桩件识别器）
    ├── test_worker.py               # 后台 worker 的调度与恢复
    ├── test_extraction_api.py       # 提取任务、路由与供应商候选接口
    ├── test_matching.py             # 分词、缩写、单向/双向相似度（对着真实描述）
    ├── test_suggestions.py          # 模糊匹配四档、最近/常用的排序与排除口径
    ├── test_merging.py              # 合并搬了什么、拦住了什么、记住了什么
    ├── test_keyword_items.py        # 关键词提取、分类词表、未分类锚点、needs_review
    ├── test_units.py                # 单位换算、制式、包装规格原文的拒绝口径
    ├── test_prices_service.py       # 标准化价格与统计的业务规则（含 09.10 算例）
    └── test_prices_api.py           # 价格接口的 HTTP 口径
```

`services/` 与 `repositories/` 中目前有 `ingredients.py`、`purchases.py`、`invoices.py` 与（服务层的）`spreadsheets.py`：没有调用方的业务文件不预先创建空壳，`units.py` 与 `prices.py` 在步骤 09 才出现。表格这一摊拆成三层各一个文件——`integrations/spreadsheets.py` 只管有界地读写字节、`services/spreadsheet_parsing.py` 只管列映射与解析、`services/spreadsheets.py` 组织批次与导出，于是「怎么读文件」和「怎么解释列」可以分别测试。`services/validation.py` 是步骤 05 按 rule of three 抽出来的：同一份文本与分页校验在第三个服务（发票）也要用，才合并成一处——在那之前刻意保留了那份重复。

⚠️ **步骤 10 的三个模块都在 `ingredients.py` 之上，而且不能反过来**：
`services/ingredients.py` 是单位与名称**词表的 owner**（`BASE_UNITS`、
`BASE_UNIT_SYNONYMS`、`PACKAGING_UNITS`、`normalize_name`），`units.py` 与
`matching.py` 都 import 它。所以模糊匹配**写不进 `ingredients.py`**——那会是循环
import，不是风格问题。代价是看 `ingredients.py` 会以为搜索还是步骤 03 的样子，
两边的模块文档各写了一段互相指路；取舍过程见
[docs/impl_record.md 步骤 10](docs/impl_record.md) 第 2 节。

## 4. 环境要求

- Python ≥ 3.12（实际验证版本见第 14 节）。
- 可以安装 PyPI 依赖的网络环境。
- 不需要任何外部 API、云账号或 API Key。真实 OCR 服务在步骤 07—08 才需要，缺少凭据不影响本目录安装、启动与测试。

## 5. 安装

全部命令在 `backend/` 目录下执行。

**Windows PowerShell**

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements-dev.txt
```

**Windows Git Bash**

```bash
py -3.13 -m venv .venv
source .venv/Scripts/activate
python -m pip install --upgrade pip
python -m pip install -r requirements-dev.txt
```

**macOS / Linux**

```bash
python3.13 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements-dev.txt
```

仅部署运行环境时安装 `requirements.txt`；开发与测试安装 `requirements-dev.txt`（它已引用运行依赖）。

随后复制配置示例并按需修改：

```bash
cp .env.example .env    # PowerShell: Copy-Item .env.example .env
```

`.env` 不是必需的：缺失时按第 9 节的默认值以 development 启动。已存在的进程环境变量优先，`.env` 不覆盖它们。

## 6. 入口约定

以下入口在后续每个步骤继续维护，不另立启动方式。命令均在激活虚拟环境后、于 `backend/` 目录执行。

| 用途 | 命令 | 当前状态 |
| --- | --- | --- |
| 启动应用 | `python app.py` | 可用（默认 http://127.0.0.1:5000） |
| 运行测试 | `python -m pytest` | 可用 |
| 执行迁移 | `python -m alembic upgrade head` | 可用（详见第 10 节） |

development 环境下会开启 Flask 调试模式与自动重载；testing 与 production 不开启。默认只监听 `127.0.0.1`：当前阶段没有身份认证与访问控制，不要绑定到公网地址。

`pytest.ini` 中设置 `pythonpath = .`，因此测试可以用 `import src.<模块>` 导入，不需要把项目安装成包。

## 7. 接口与错误响应

### 健康检查

```text
GET /health  ->  200
{"status": "ok", "app_env": "development", "version": "0.1.0"}
```

`/health` 只回答「进程起来了、配置解析成功了」。它**不检查**数据库与外部服务连通性——数据库边界在步骤 02 建立，真实 OCR 在步骤 08 接入，没做的检查不会出现在响应里。

### 食材与分类（步骤 03）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/categories` | 列出分类（按名称排序） |
| POST | `/api/categories` | 新增分类：`{"name": "...", "name_zh": "..."}`，`name` 必填 |
| PATCH | `/api/categories/<id>` | 改名 / 改中文名（传 `null` 表示清空中文名） |
| DELETE | `/api/categories/<id>` | 删除；仍被食材引用时 409 |
| GET | `/api/food-items` | 搜索与列表，参数见下 |
| GET | `/api/food-items/suggest` | **候选建议**：模糊匹配 / 最近 / 常用（步骤 10，见下） |
| POST | `/api/food-items` | 快速新增；必填 `name`、`category_id`、`base_unit` |
| GET | `/api/food-items/<id>` | 单条详情 |
| PATCH | `/api/food-items/<id>` | 局部修改；未出现的字段保持原值，传 `null` 清空可选字段 |
| DELETE | `/api/food-items/<id>` | 无单据入库关联时连同独立采购历史删除；已有单据入库记录时 409 |
| POST | `/api/food-items/<id>/merge-preview` | **合并预览**：会搬动什么、什么拦着（只读，步骤 10） |
| POST | `/api/food-items/<id>/merge` | **执行合并**（一次事务；⚠️ 不可逆，步骤 10） |

**快速新增只要求三个字段**（`docs/imp_notes.md`：不要让老板一次填一堆）：

```text
POST /api/food-items
{"name": "Chicken Breast", "category_id": 1, "base_unit": "lbs"}
->  201
{"id": 1, "name": "Chicken Breast", "name_zh": null, "specification": null,
 "base_unit": "lb", "category": {"id": 1, "name": "Meat & Poultry", "name_zh": "肉类与禽类"},
 "created_at": "2026-11-10T02:31:07.947681+00:00", "updated_at": "..."}
```

`name_zh`、`specification` 可选填；采购来源不在这里填，它随采购记录在步骤 04 出现。

**搜索**：`GET /api/food-items?q=&category_id=&limit=&offset=`

- `q` 同时匹配英文名、中文名与规格，一条食材只返回一次；大小写、全角半角、多余空格都不影响命中（`牛肉末`、`ground beef`、`GROUND  BEEF`、`80/20` 找到的是同一条）。
- `%`、`_` 按字面处理，搜 `100%` 不会变成「匹配全部」。
- 排序为精确命中 → 前缀命中 → 其余，同档按名称，分页结果稳定。
- 响应为 `{"items": [...], "total": n, "limit": n, "offset": n}`；`limit` 默认 20、范围 1—100，`offset` ≥ 0。
- `never_purchased=true` 筛出**一条采购记录都没有引用过**的食材（步骤 10.15，
  用来回收孤立食材）。判据包含已作废的采购记录；这个筛选只说明采购历史，
  不代表是否允许删除。删除时，独立采购及其修改历史会一并清理；已有单据入库关联则拒绝删除。
- ⚠️ **这个接口刻意不做模糊匹配**（步骤 10 的决定）。它是食材库那一页：分页浏览、
  找确定的某一条，然后改它或删它——在那里「这条到底在不在库里」必须有确定答案，
  所以搜 `CNTRY CROCK` 返回 **0 条**是正确行为。模糊匹配在下面那个接口。

**候选建议**（步骤 10）：`GET /api/food-items/suggest?q=&category_id=&limit=`

这是**选食材那一屏**用的接口，与上面的分页列表分工不同（理由见
`docs/impl_record.md` 步骤 10 第 4 节）。带 `q` 做四档匹配，不带 `q` 给「最近 + 常用」：

```text
GET /api/food-items/suggest?q=CNTRY%20CROCK
->  200
{"query": "cntry crock",
 "matches": [{"food_item": {...}, "match_reason": "fuzzy", "score": 0.9607,
              "purchase_count": null, "last_purchase_date": null, "merged_from": null}],
 "recent": [], "frequent": [], "notes": []}
```

- `match_reason` 六种：`exact` / `prefix` / `contains`（这三档与步骤 03 的排序同
  口径）、`fuzzy`（模糊命中）、`merged_from`（搜的是一个**被合并掉的旧叫法**，给出
  的是它现在归属的那条）、以及不带 `q` 时的 `recent` / `frequent`。
- **排序先按档位、再按分数**。`score` 只表示"有多像"，不负责把精确命中压到模糊
  命中后面去——`CNTRY CROCK` 的模糊分能到 0.96，比包含命中还高。
- **容忍三类真实写法**：标点不同（`CHIX-BRST` ↔ `CHIX BRST`）、抽掉元音的缩写
  （`CNTRY` ↔ `COUNTRY`，来自真实小票）、拼写错误（`beff` ↔ `beef`）。
  ⚠️ 不引向量库、不引 Embedding API，只用 `unicodedata` + `difflib`。
- **不带 `q`** 时 `recent` 按**最近一次入账**排（不按纸面日期——步骤 04 允许日期
  留空，按日期排会让那些记录永远沉底），`frequent` 统计最近 90 天的采购次数。
  ⚠️ **未填日期的记录进不了常用榜**（与步骤 09 的月度均价同一条口径），这件事由
  `notes` 说出来。
- `notes` 必须跟着列表一起读：扫描触顶、常用窗口排除了什么都在里面。
- ⚠️ **分数再高也不会替人选中。** 零点击选中走的是**精确命中**那条路（见下面的
  发票行自动认食材），不吃模糊分数——`pork` 与 `park` 的比值是 0.75。

**重复食材合并**（步骤 10）：`POST /api/food-items/<id>/merge-preview` → `.../merge`

路径里的 `<id>` 是**保留下来**的那一条，body 的 `source_ids` 是被并掉的那些
（方向写进 URL，不靠 body 里两个长得一样的字段来区分谁吞并谁）：

```text
POST /api/food-items/1/merge-preview   {"source_ids": [2]}
->  200
{"target": {...}, "sources": [{"food_item": {...}, "purchase_count": 7,
                               "invoice_line_count": 3, "pack_spec_count": 1,
                               "duplicate_pack_spec_count": 0}],
 "can_merge": true, "blockers": [],
 "moved_purchase_count": 7, "moved_invoice_line_count": 3, "moved_pack_spec_count": 1,
 "notes": ["⚠️ **合并不可逆**：…"]}
```

- 搬动 `purchases` / `invoice_lines` / `pack_specs` 三处外键，**一条都不删、一个
  数值都不改**——`original_description`、数量、单价、金额、日期、作废标记全部原样
  保留，发票行的 `version`（乐观锁）也不动。
- **三种冲突拒绝而不是挑一个**（409，`details.blockers` 带 `code`）：
  `pack_spec_conflict`（两边对同一个来源 + 包装单位各有一条不同的箱规——「一箱
  等于多少」是人确认过一次的事实，系统不替他挑）、`incompatible_base_unit`
  （lb 与 gallon 不是同一类计量，没有密度不能换）、`self_merge`。
  被拦住时**什么都没动**，不做"能并多少并多少"。
- **同一类计量、不同单位**（lb 与 kg）允许，但 `notes` 会说明：历史价格会换一个
  单位显示，**金额没变，变的是每单位的量**。
- ⚠️ **合并不可逆**，这句话固定出现在每次预览的 `notes` 里。系统会记下旧叫法
  （之后按旧叫法仍然搜得到，`match_reason="merged_from"`），但**分不清哪几条采购
  原本属于哪一边**，没有一键还原。
- 一次最多并 10 条——不是性能上限，是手滑上限。

**基础单位**是固定白名单：`lb`、`oz`、`g`、`kg`、`gallon`、`qt`、`pt`、`fl_oz`、`l`、`ml`、`each`、`dozen`。常见写法会归一（`lbs`/`Pounds`/`磅` → `lb`，`个`/`ea` → `each`，`fl oz` → `fl_oz`）。`case`、`pack` 等**包装单位会被拒绝**并给出单独说明——没有包装规格就换算不出可比价格，包装规格与换算属于步骤 09。

**重复名称直接拒绝**，并指出已存在的那一条，便于前端改用它：

```text
POST /api/food-items   {"name": "  ground   beef ", ...}
->  409
{"error": {"code": "conflict",
           "message": "已存在同名食材：Ground Beef（牛肉末）。可以直接使用它，或改用其它名称。",
           "details": {"field": "name",
                       "existing": {"id": 1, "name": "Ground Beef", "name_zh": "牛肉末"}}}}
```

名称与中文名在**规范化后全局唯一**（由数据库唯一约束保证）。已经建重了的那些，
用上面的合并接口收拾（步骤 10）。

**`needs_review`**（步骤 10.13）：食材响应里多一个布尔字段，为真表示**这条是系统
按关键词自动建出来的、还没人确认过**——名字是从供应商描述里剥的、分类是本地词表
猜的，两样都可能不对。界面要把它和老板自己建的食材区分开。老板**改过它**、
或**用它确认过任意一行**之后自动清掉。

**未知字段会被拒绝**（400）：把 `name_zh` 写成 `nameZh` 时如果静默忽略，用户填的中文名就被悄悄丢掉了。时间戳一律是带 `+00:00` 的 UTC：没有偏移量的时间串会被浏览器按本地时区解释。

所有错误（业务异常与框架错误）都返回同一种结构：

```json
{"error": {"code": "validation_error", "message": "数量必须大于 0", "details": {"field": "quantity"}}}
```

`details` 只在确实有字段级信息时出现。响应里不包含异常堆栈、内部实现细节与请求 URL 回显。

| 异常 | 状态码 | `code` | 用途 |
| --- | --- | --- | --- |
| `ValidationError` | 400 | `validation_error` | 缺字段、数值无效、单位无法解析 |
| `NotFoundError` | 404 | `not_found` | 资源不存在 |
| `ConflictError` | 409 | `conflict` | 重复确认、重复提交、版本冲突 |
| `FeatureNotImplementedError` | 501 | `not_implemented` | 能力尚未实现，明确告知而非假成功 |
| `AppError`（基类） | 500 | `internal_error` | 未分类业务异常 |
| 框架错误（werkzeug） | 原状态码 | 由异常名生成，如 `method_not_allowed` | 404、405、400、415 等 |
| 未预期异常 | 500 | `internal_error` | 堆栈只进日志，响应为通用文案 |

异常类定义与映射都在 [src/errors.py](src/errors.py)。业务代码只抛这些异常，不自己拼 HTTP 响应、不设置状态码。

### 采购记录（步骤 04）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/purchases` | 查询采购历史，参数见下 |
| POST | `/api/purchases` | 手工录入；必填 `food_item_id`、`quantity`、`purchase_unit`、`unit_price`、`line_amount` |
| GET | `/api/purchases/<id>` | 单条详情 |
| PATCH | `/api/purchases/<id>` | 直接修改（含数值），自动记修改日志 |
| DELETE | `/api/purchases/<id>` | **作废**（软删，返回 200 而不是 204） |
| GET | `/api/purchases/<id>/revisions` | 修改历史：每次改动前后的值 |

**数值一律用字符串**。`quantity` / `unit_price` / `line_amount` 在响应里是字符串，请求也应该用字符串；传 JSON 小数会被明确拒绝。原因是 JSON 的数字是双精度浮点，`1234567.89` 经前端 `JSON.parse` 一转就可能差一分钱——而这正是步骤 04 要解决的问题。整数可以直接传。

```text
POST /api/purchases
{"food_item_id": 1, "quantity": "10", "purchase_unit": "lb",
 "unit_price": "6.00", "line_amount": "60.00",
 "purchase_date": "2026-11-08", "source_name": "Costco",
 "original_description": "GRND BF 80/20"}
->  201
{"id": 1, "food_item": {"id": 1, "name": "Ground Beef", "name_zh": "牛肉末", ...},
 "purchase_date": "2026-11-08", "source_name": "Costco",
 "original_description": "GRND BF 80/20",
 "quantity": "10", "purchase_unit": "lb", "purchase_unit_normalized": "lb",
 "unit_price": "6.00", "line_amount": "60.00", "currency": "USD",
 "amount_mismatch": false, "entry_source": "manual",
 "source_invoice": null, "voided_at": null, "revision_count": 0,
 "created_at": "...", "updated_at": "..."}
```

`purchase_date`、`source_name`、`original_description`、`currency` 都可以不填。`source_invoice` 现在恒为 `null`：发票表在步骤 05 才出现，字段先占位，那一步就不用改响应结构。

**查询**：`GET /api/purchases?food_item_id=&source=&date_from=&date_to=&undated=&include_voided=&limit=&offset=`

- 排序为 `purchase_date DESC, id DESC`。第二个键不是装饰：同一天的多条采购必须有稳定次序，否则同一个 `offset` 两次请求可能漏行或重行。
- SQLite 把 NULL 视为最小值，所以未填日期的记录自然排在最后，不会挤在最新采购前面。
- `undated=true` 筛出**待补日期清单**，`undated=false` 只要能进月度统计的那些。没有这条筛选，未填日期的记录会悄悄从价格分析里消失。
- `source` 按规范化后的来源匹配：`Costco` / `costco` / `  COSTCO ` 是同一个来源。
- `include_voided=true` 才会带出已作废的记录。
- 分页同步骤 03：`limit` 默认 20、上限 100，响应给 `items/total/limit/offset`。

**行金额是权威**。`|数量 × 单价 − 行金额| > $0.05` 时返回 `amount_mismatch` 错误码（HTTP 400）：

```text
POST /api/purchases    {"quantity": "3", "unit_price": "10.00", "line_amount": "35.00", ...}
->  400  {"error": {"code": "amount_mismatch", "message": "...",
      "details": {"expected": "30.00", "line_amount": "35.00",
                  "difference": "5.00", "tolerance": "0.05"}}}

POST /api/purchases    {..., "accept_amount_mismatch": true}
->  201  {..., "line_amount": "35.00", "amount_mismatch": true}
```

容差只盖供应商按分舍入那个量级（11.3 lb × $6.35 = $71.755，单据上印 $71.76，差 $0.005 直接放过，不烦老板）；真印错的那种差得远不止这个量级，拦下来让人核对。确认后**按行金额入账**，不拿单价反算。

**纠错是直接修改 + 修改日志**（2026-11-10 用户确认，不强制作废重录）：

```text
PATCH /api/purchases/1   {"unit_price": "8.50", "line_amount": "85.00", "reason": "单价看错了"}
->  200  {..., "unit_price": "8.50", "revision_count": 1}

GET /api/purchases/1/revisions
->  200  {"items": [{"id": 1, "changed_at": "...", "action": "update",
      "changed_fields": {"unit_price": {"before": "5.80", "after": "8.50"},
                         "line_amount": {"before": "58.00", "after": "85.00"}},
      "reason": "单价看错了"}]}
```

日志里的数值是**字符串**，不经 JSON 浮点中转——否则日志自己就会丢精度，而它恰恰是用来解释精确数字的。什么都没改时不写日志，「点了保存但没改东西」不该在历史里留一条空记录。

**`DELETE` 是作废而不是删除**：记录仍在（`include_voided=true` 能查到），只是 `voided_at` 有了值。不硬删的原因是步骤 05 的发票行要能追溯到它；已作废的记录不能再修改（409）。

**退货 / 折让 / 其它调整行**（2026-11-10 追加）：传 `line_kind: "adjustment"`。

```text
POST /api/purchases
{"line_kind": "adjustment", "food_item_id": 1, "quantity": "-2",
 "purchase_unit": "lb", "unit_price": "6.00", "line_amount": "-12.00",
 "original_description": "RETURN 2LB"}
->  201  {"line_kind": "adjustment", "quantity": "-2", "line_amount": "-12.00", ...}

POST /api/purchases                      # 整单折让：不指向任何食材
{"line_kind": "adjustment", "quantity": "1", "purchase_unit": "each",
 "unit_price": "-20.00", "line_amount": "-20.00",
 "original_description": "EARLY PAY DISCOUNT"}
->  201  {"food_item": null, "line_amount": "-20.00", ...}
```

规则：

- 调整行的数量、单价与行金额**可以为负**，`food_item_id` **可以不填**（整单折让在单据上不指向任何一种食材，强行挂到某个食材上会污染那个食材的采购历史）。
- 调整行的 `line_amount` **不能为 0**（0 元的退货或折让没有意义）。
- 其余校验与采购行完全一致：「数量 × 单价 ≈ 行金额」照样要过，所以 `-2 × $6.00 = -$12.00` 自洽，而 `-2 × $6.00` 对 `-$50.00` 会被 `amount_mismatch` 拦下。
- 正常采购行**仍然不收负数、仍然必须有食材**；拒绝时的消息会指向 `line_kind: "adjustment"`，而不是只说「不行」。
- `line_kind` **不能用 `PATCH` 改**（会返回未知字段 400）。录错了类别请作废重录——原地改类别会让同一条记录的历史跨越两种语义。

**`kind` 默认只返回采购行。** `GET /api/purchases` 的 `kind` 取 `purchase`（默认）/ `adjustment` / `all`：

```text
GET /api/purchases                       # 只有采购行，适合看价格历史
GET /api/purchases?kind=all              # 对账：各行相加应等于单据总额
```

默认排除而不是默认包含，是因为两种错法的代价不对称：**对账时漏了调整行，各行相加对不上总额，一眼看得出；而把一笔退货混进加权均价只会让价格悄悄偏低，没人会发现。** 按食材查历史（`?food_item_id=`）同样默认排除。步骤 09 的所有价格统计也只取 `line_kind = "purchase"`，这条写进了它的规格。

**仍然不做的**：库存调整、退款管理、完整退货流程（审批、关联原单、退款状态）。调整行只是**一条能表达负数的记录**，用于对账；它不自动冲减任何数量，也不指向被退的那一笔原始采购。

**本轮不支持的**：单位换算与标准化价格属于步骤 09，所以现在只能看到**原始计价单位**的价格。0 元行（赠品、买十送一）可以正常录入。

### 发票审核（步骤 05）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/invoices` | 上传文件并建草稿（**multipart**，字段名 `files`，可重复；`recognize=false` 则只存不识别） |
| GET | `/api/invoices` | 单据列表；`completed=false` 就是待办清单，另有 `source` / `date_from` / `date_to` / `order`（`created_at` 默认 / `invoice_date`）/ `include_voided` / `limit` / `offset` |
| GET | `/api/invoices/<id>` | 详情：文件、行、每个待确认行的匹配候选、对账结果、可能重复的单据 |
| PATCH | `/api/invoices/<id>` | 改单据头（来源、单号、日期、币种、总额，全部可空） |
| DELETE | `/api/invoices/<id>` | **作废**整张单据；有已确认行时须提交当前 `confirmed_line_ids`，返回 200 |
| POST | `/api/invoices/<id>/complete` | **收口确认**：审完所有行且填写单据日期后点一次，响应里给出行数、文件数与差额；缺日期返回 409 |
| POST | `/api/invoices/<id>/reopen` | 将已完成单据重新打开审核；保留已入库记录，返回最新单据；未完成或已作废时返回 409 |
| POST | `/api/invoices/<id>/files` | 补传文件（漏拍一页、重拍一张） |
| DELETE | `/api/invoices/<id>/files/<file_id>` | 删掉拍坏的文件（**不能删到一个不剩**；正在识别时 409） |
| GET | `/api/invoices/<id>/files/<file_id>/content` | 回看原件，原样返回存下来的字节 |
| POST | `/api/invoices/<id>/lines` | 手工加一行（所有字段都可以先留空） |
| PATCH | `/api/invoices/<id>/lines/<line_id>` | 改一行：改值、改食材、改类别，或标成 `skipped` / 改回 `pending` |
| DELETE | `/api/invoices/<id>/lines/<line_id>` | 删掉**手工加的**行；识别出来的行请用 `skipped` |
| POST | `/api/invoices/<id>/confirm` | 确认选定行（`lines`）或所有待确认行（`all: true`） |
| POST | `/api/invoices/<id>/lines/<line_id>/unconfirm` | 撤销一行的确认 |
| GET | `/api/invoices/match` | 问「这句原始描述以前确认到哪个食材」（`description` 必填，可带 `source`） |

**上传。** `multipart/form-data`，文件字段名固定是 `files`：

```text
POST /api/invoices
  files=@IMG_0421.jpg  files=@IMG_0422.jpg     # 多页单据一次传
  recognize=false                              # 可选：只存文件，老板对着照片手工加行
```

类型白名单 `jpg` / `jpeg` / `png` / `pdf` / `heic`；**HEIC 只保存不识别**（iPhone 原图常常是它，保留原件是本步骤的职责，解码留给步骤 08）。单文件上限由 `UPLOAD_MAX_BYTES` 决定（默认 20 MB），一次请求的总上限是它的 4 倍，超了由 Flask 返回 413（错误结构仍是统一那一种）。一张单据最多挂 20 个文件。

**重复上传。** 两种口径，轻重不同：

- **完全相同的文件**（`sha256` 相同）→ 409，`details.invoice_id` 指回已有的那张单据，让老板去那边接着审；
- **来源 + 单号 + 总额都相同但文件不同** → 不拦，详情里给 `possible_duplicate_invoice_ids`。同一张单补拍的照片字节本来就不同，拦住反而误伤。
- **没有单号时，用采购日期补它的位置**：来源 + 采购日期 + 总额都相同，且**两边都没有单号**，同样给出这条软提示。读不出单号就一页一张（第 12 节的单据边界），同一张单重复上传时便没有单号这条线索了。只凭「来源 + 总额」会在同一家供应商的常规订货上天天误报，加上同一天才足够紧。两边都必须没有单号：一张读出了、一张没读出，说明纸面上本来就分得开。缺日期又缺单号则不判重。

**删整单。** 界面先确认删除；若有已入账行，再提示具体行数。请求 `DELETE /api/invoices/<id>` 带 JSON `{"confirmed_line_ids": [11, 12]}`，必须与服务端当前已确认行的 ID 集合完全一致；未带或状态已变化则返回 409。成功时，同一事务里作废这些行的采购记录（保留修改日志），再作废单据、删除原件。已作废采购记录不进入价格统计；原始文件删除后无法再回看，但同一文件可以重新上传。没有已入账行时仍可不带请求体删除。

> **批次来的单据：整理页上的那几页一并真删**（2026-11-24 反馈，`docs/multifiles.md` 第 17 节 ④）。同一个事务里按序删掉撤销日志里指向这些内容的条目、送审链接、内容本身（候选随 ORM 级联），顺序与移除文件那条路（第 16 节）一致。于是「删除本张单据」之后，整理页上不会再留下一张「草稿已作废」的卡片。⚠️ **不删的两样**：`intake_files` 里的字节与磁盘上的原件——同一份 PDF 的其它页可能还在整理页上；单据行本身仍是**作废**而不是抹掉（它可能背着已作废的采购记录）。代价写在这里：那几页**回不来了**，要重新整理得把那份文件重新上传一次（重传按全新的一份处理）。

**删文件。** 三道闸门都回 409 并说清为什么：① 单据上**最后一个文件**不能删（发票至少要留一份原始凭据）；② 表格导入批次的**源文件**不能删（它就是那些明细的凭据，要撤就作废整个批次）；③ 这个文件**正在识别**时不能删——提取任务还在排队或正在跑，worker 已经按它的 `storage_key` 去读字节，删掉只会换来一条看不懂的失败，而那次模型调用的钱照样要花（`details` 里给出 `extraction_job_id` 与 `status`，等它跑完或失败再删）。删成功时磁盘上那份字节一并删除；**已经跑完的提取任务记录保留**，只把 `invoice_file_id` 置空并在 `warnings` 里记下被删的是哪个文件——任务历史是事后唯一能解释「这张单当初怎么没出行」的东西；**已经识别出来的草稿行也不跟着删**（人可能已经改过它们），它们的证据里仍记着那个文件编号，界面据此提示「这一行的原件已经被删掉了，别拿左边那张纸核对它」。

**一行的两套值。** 业务列（`description` / `quantity` / `purchase_unit` / `unit_price` / `line_amount`）是**当前待确认值**，可为 `null`——认不出来的格子就是空的，**不会变成 `"0"`**。`raw_fields` 是**原始识别证据**（每个字段的原文、`recognized` / `unreadable` / `missing` 状态、置信度、页码与区域坐标），确认之后也不改动。`missing_fields` 直接告诉前端这一行还差什么。

**确认。** 请求体逐行给选项，返回 **200** 并逐行给结果：

```text
POST /api/invoices/7/confirm
{"lines": [
  {"line_id": 11, "food_item_id": 3},
  {"line_id": 12, "new_food_item": {"name": "Jasmine Rice", "category_id": 5, "base_unit": "lb"}},
  {"line_id": 13, "accept_amount_mismatch": true, "version": 2}
]}
→ 200 {"confirmed": 2, "failed": 1, "results": [...], "invoice": {...}}
```

- **一行失败不影响其它行**（每行一个事务）。「三行确认两行、第三行报错」是正常结果，所以 HTTP 是 200 而不是 4xx；整个请求级别的错误（单据不存在、整张单作废）才用 4xx。
- `new_food_item` 可以**当场新建食材**（只要名称、分类、基础单位），与确认同一个事务：确认失败就不会留下一条谁也没要的食材。
- 前端首次采购改用 `new_food_item: {"name": "...", "category_id": 5, "use_purchase_unit_as_base": true}`：服务端从该行采购单位归一出首个单位，同名已有食材直接复用；包装单位可暂作首个单位，但没有箱规时不计算标准化单价。普通食材新增接口仍要求显式基础单位。
- 金额校验口径与步骤 04 完全一致：差额超 $0.05 先报 `amount_mismatch`，带 `accept_amount_mismatch: true` 重发才入账并标记。
- `version` 是行上的乐观锁，给了就检查：两个标签页同时审同一行，后一个会收到 409 而不是悄悄覆盖。
- 确认时**日期 / 来源 / 币种从单据头继承**，行上有覆盖值则用行上的；之后修正单据头会同步仍沿用原单据头值的已入账采购，并写入采购修改日志。行内覆盖值和单独修正过的采购值保持原样。

**状态。** 行只有三态 `pending` / `confirmed` / `skipped`（跳过要带一句原因）；单据状态是**派生值**、数据库里没这一列：`draft` → `in_review` → `ready_to_complete` → `completed`，另有 `voided`。`ready_to_complete` 与 `completed` 分开，是为了保住收口确认那一次点击。

**对账。** 四态，两种「没法对」不显示成 0 差额：

| `reconciliation.status` | 含义 |
| --- | --- |
| `not_checked` | 还有 `pending` 行，先不对账（算了只会报假警） |
| `no_total` | 单据总额没识别出来，没法对 |
| `balanced` | 差额在 $0.05 容差内 |
| `mismatch` | 差额超容差——**只提示，不阻断**任何确认，也不阻止完成 |

对账金额取**采购记录上**的行金额（那是真正入账的数字），并且**包含 `adjustment` 行**：税费、运费、押金、退货与折让都算进来，各行相加才等于单据总额。

**追溯。** 确认出来的采购记录带 `source_invoice: {invoice_id, invoice_line_id, line_no}`，顺着它能 `GET /api/invoices/<id>` 看原件；手工录入与表格导入的记录是 `null`（它们没有单据）。

**本轮不做**：发票审批流、强制供应商 / 单号 / USD、置信度分级与区域高亮的界面交互（前端任务）、真实 OCR（07—08）、模糊匹配（10）、换算与价格统计（09）。

### 表格导入与导出（步骤 06）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/imports/preview` | **只读预览**：看列映射对不对、哪几行有问题，不写数据 |
| POST | `/api/imports` | 建一个待审核批次（`multipart`，文件字段 `file`，选项放在 `options` 这个 JSON 字符串里） |
| GET | `/api/imports/template` | 下载中文表头模板（`format=csv` / `xlsx`） |
| POST | `/api/invoices/export` | JSON `{ "format": "xlsx" 或 "csv", "invoice_ids": [1, 2] }`；只接受 1–200 张已完成且未作废单据，导出所选单据当前有效的采购行，不含调整行；最多 10000 行 |

**导入就是步骤 05 的草稿。** 批次是一张 `entry_source='import'` 的单据：原始表格文件原样存着、每行是一条 `pending` 的待确认行，修改、匹配食材、逐行确认、撤销、收口确认全部用上面发票那一组接口；入账走步骤 04 的 `create_purchase`，记录的 `entry_source` 是 `import`。**导入不调识别器**——表格本来就是结构化的，走 OCR 只会凭空引入错误。

### 多文件整理中的表格入口（M04）

已有整理批次 ID 后，可用 `POST /api/intake-batches/<id>/spreadsheets/preview` 预览，再用 `POST /api/intake-batches/<id>/spreadsheets` 上传；两者均接收 `multipart` 的 `file` 与 JSON 字符串 `options`，选项与旧 `/api/imports` 一致。预览另接 `limit`、`offset`，返回 `total` 及当前页的行。上传将 CSV/XLSX 的每一条非空原始行建成独立的 `intake_items` 内容，返回 `batch_file_id`、行数、摘要与重复提示；**此时不生成发票或采购记录**。日期/来源列的解析值和原单元格分别持久化，歧义或无效值留在该行的问题清单中，人工分类决定由整理命令写入，结构化候选生成属于 M05。

| 方法 | 路径后缀（前缀 `/api/intake-batches/<id>/spreadsheets`） | 用途 |
| --- | --- | --- |
| GET | `/rows?limit=50&offset=0` | 分页读取行，最多 200 条；`total` 为总行数 |
| GET | `/rows/<item_id>` | 读取一行的原行号、解析值、单元格原文和逐行问题 |
| GET | `/groups` | 按人工决定的日期与规范化商家名汇总行数 |
| GET | `/<batch_file_id>` | 读取文件状态、行数、列映射和导入选项 |
| GET | `/<batch_file_id>/original` | 下载原表格字节 |

跨批次或旧导入路径已有相同字节时，上传先返回 409 和关联批次/单据 ID；用户明确提交 `confirm_duplicate=true` 后才加入新批次。同一批次重复文件仍返回 409。保存行失败时文件记为 `failed`，再次上传同字节可重试。旧 `/api/imports` 接口仍按整份表建一张待审核单据，供原流程使用。

### 多文件整理批次：图片 / PDF 的摄入（M03）

一次上传十几份混合文件时，先建一个**整理批次**，再**逐个**把文件传进去。批次不是一张发票：一份 PDF 的不同页可以落到不同的日期/商家组，所以摄入只产生「可归类内容」（图片一张算一页、PDF 按页），**不在上传时建任何单据**。

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| POST | `/api/intake-batches` | 新建一个整理批次 |
| GET | `/api/intake-batches/current` | **恢复**：最近一个仍开放且有未移除文件的批次；空批次不构成待办，返回 404。等待处理或处理失败的文件仍可恢复。 |
| GET | `/api/intake-batches/<id>` | 批次与逐文件处理状态（**轮询这个**）；另带 `item_count` / `unfinished_item_count` |
| POST | `/api/intake-batches/<id>/close` | 这一批填完了：收口（`/current` 不再返回它）；已收口的批次再收一次是 409 |
| GET | `/api/intake-batches/<id>/items?limit=50&offset=0` | 分页读可归类内容，最多 200 条；M11 可按日期、商家、待定原因筛选 |
| POST | `/api/intake-batches/<id>/files` | **一次一个文件**（`multipart`，字段 `file`） |
| POST | `/api/intake-batches/<id>/process` | 驱动切页队列；`{"retry_failed": true}` 连失败的一起重试 |
| DELETE | `/api/intake-batches/<id>/files/<file_id>` | 把一个文件移出批次：**连同它的页与分类一起删**（不可反悔；有内容已进填表时 409） |
| GET | `/api/intake-batches/<id>/files/<file_id>/original` | 回看整份原件 |
| GET | `/api/intake-batches/<id>/items/<item_id>/original` | 回看某一页的原件 |
| DELETE | `/api/intake-batches/<id>/items/<item_id>` | 删除未开始填表的一页/一行；请求 `{"version":N}`，版本变化或已送入草稿时返回 409。删除文件的最后一项时一并移除该批次文件引用，重传从头开始。已开始填表的内容须删除对应整张单据。 |

M11 财务表格沿用 `/items`，增加可选的 `purchase_date=YYYY-MM-DD`、`source_name=<确认名>` 和 `missing=any|both|date_any|source_any|date|source|recognition_failed`，筛选在数据库分页前进行，`total` 是筛选后的总数。`date_any` / `source_any` 包含两项都缺的内容，`date` / `source` 只含单独缺该项的内容；另可传 `purchase_date=missing` 或 `source_name=missing` 精确筛空值。表格行的响应新增 `row_values`（保存的解析字段；数量和金额仍是字符串），图片/PDF 页为 `null`；`row_no` 是原 CSV/XLSX 行号，不是分页序号。没有迁移，分类写入仍走 M06 `/moves`，送审仍走 M08 `/reviews`。

**这一批填完了就收口。** `POST .../close` 把批次置为 `closed`：它不再接收新文件，`GET /api/intake-batches/current` 也不再返回它，因此首页那个「接着整理」消失——这就是「此次多文件填表结束」（见 [docs/multifiles.md 第 22 节](docs/multifiles.md)）。批次本身仍然读得到（URL 里那个 `batch=<id>` 仍然有效，只能查看）。⚠️ **服务端不检查是否全部填完**：第 5 节允许把待定内容留着以后再办。「全部填完才按得动」是整理页上的一道闸，它问的是批次详情里那两个**派生计数**：`item_count`（这一批有几项可整理内容）与 `unfinished_item_count`（其中没有落在一张**已完成**单据里的有几项）。两个数都不落表：填没填完由那几页所在的单据说了算。没有迁移。

**传错的文件撤得下来，而且是真删。** `DELETE .../files/<file_id>` 把这份文件在这个批次里的记录一并删掉：撤销日志里指向这些内容的条目（空掉的那次移动一并删）→ 送审链接 → 内容与候选 → 这条文件引用本身。⚠️ **不可反悔**（2026-11-24 改掉了原来的软删）：同一份文件再传进来是**全新的一份**，新 id、新传入时刻、新切出来的页，`decided_*` 全空。响应的 `removed_item_count` 是跟着删掉的内容数，`file` 是删之前那条引用的样子。盘上的字节不在这里删（别的批次可能还在引用同一份），`bytes_still_referenced` 只回答「现在还有没有人用它」。⚠️ `discarded` 状态与 `include_discarded` 参数留着只为读 2026-11-24 之前标成 `discarded` 的旧行，新的移除不再产生这种行。⚠️ 已经有内容进入填表的文件**撤不掉**（409）：草稿是对着这份原件填的，原件必须留着供核对。409 的 `details.handed_off_item_count` 说出有几项，消息指向下一步——先在整理页撤回填表，已入库的走单据里的撤销确认与采购修订。同一个数字也在批次详情的每个文件上透出，上传页据此把「移除」按钮提前暗下来并写出原因，而不是让人点一下才撞到 409（见 [docs/multifiles.md 第 14 节](docs/multifiles.md)）。

**上传只存字节、只登记，不等切页。** 响应里的 `queued` 说明切页是不是交给后台了：是的话 `ingest` 为 `null`，前端去轮询批次状态。⚠️ **状态只有 `pending` / `ready` / `failed` / `discarded` 四个离散值，加上「切出了几个内容」（`item_count`）与「其中几个已经开始填表」（`handed_off_item_count`），没有百分比**——本地切页没有可信的中间进度，编一个只会让人以为卡在 73%。实测 12 MB / 32 页 PDF：上传请求 0.07 秒返回，切页 0.30 秒后完成。

**一次一个文件**（字段 `file`，不可重复），与 `POST /api/invoices` 的 `files`（同一张单据的多页）刻意不同：这里十几份文件属于不同的单据，整批一起提交的话一份超限就得全部重传。

**收不收按扩展名，怎么切按字节。** 白名单与单张上传同一份（`jpg` / `jpeg` / `png` / `pdf` / `heic`，CSV / XLSX 走上面 M04 的表格入口），而真正按什么格式切页由内容嗅探决定——`.jpg` 里装着三页 PDF 会被切成三页。解析失败不是 500，而是**写在文件上的状态**：`failed` + `error.code`（`pdf_unreadable` / `image_unreadable` / `content_unsupported` / `bytes_missing`）+ 一句能读的原因，坏掉一个文件不影响同批次其它文件，重试走 `/process`。进程被 kill 后没切完的文件停在 `pending`，恢复批次时调一次 `/process` 就接着切。

**回看原件原样返回存下来的字节**，不压缩、不转格式——它是核对用的证据。⚠️ PDF 的某一页返回的是**整份 PDF**，页号在 `X-Intake-Page` 响应头里（还有 `X-Intake-Row`、`X-Intake-Item-Kind`）：本项目没有 PDF 栅格化依赖，后端生成不出「第 3 页那张图」，由前端的查看器跳页；返回占位图冒充原件是不行的。

**同一份字节只存一次。** 同一批次里重复的文件返回 409 并指回已有的那一条；别的批次里也有同一份字节时只在响应里提示（`shared_with_batch_ids`），旧单张上传路径上已有单据时同样只提示（`same_bytes_invoice_ids`）——两条路径的判重是分开的，互相不阻断。

**摄入阶段不决定商家，也不决定日期。** 刚切出来的内容 `decided_*` 全空、候选为 0，都留在「待归类」里。日期/商家候选是 M05，归组是 M06。

### 多文件整理中的人工归组（M06）

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/api/intake-batches/<id>/groups?limit=50&offset=0` | 分页读取派生组与各组内容数，最多 200 组 |
| GET | `/api/intake-batches/<id>/groups/items?purchase_date=2026-11-08&source_name=Costco&limit=50&offset=0` | 分页读取指定组；两项都省略为待归类，只给日期为该日商家待定 |
| POST | `/api/intake-batches/<id>/moves` | 一次提交多张卡片的分类决定 |
| POST | `/api/intake-batches/<id>/moves/<move_id>/undo` | 撤销最近一次未撤销移动 |

移动请求例子：`{"request_key":"唯一重试键","action":"group","items":[{"id":1,"version":1},{"id":2,"version":1}],"purchase_date":"2026-11-08","source_name":"Costco"}`。`action` 可为 `date`（仅改日期，原来源保留）、`group`（同时改日期和来源）、`clear`（清空日期和来源）、`exclude` / `include`（移出／恢复建议堆例外标志）。一次最多 200 张；同键同参数重发返回原操作，同键不同参数或旧版本冲突返回 409。整批在一个事务内成功或回滚。列表里的 `candidates` 是系统建议与依据，`decided_*` 是人工决定；没有确认商家时在日期下的「商家待定」。M08 送审在同一事务设置 `handed_off_at`，之后本接口拒绝直接改归属。

### 整理内容送入现有审核（M08）

日期/商家组只是工作队列，不是一张发票。调用方明确选择内容组成一张草稿：`POST /api/intake-batches/<id>/reviews`，请求 `{"items":[{"id":1,"version":2}]}`；要把后续页并入同一张待审核草稿，加 `target_invoice_id`。同批、同已确认日期与商家才可合并；不同单号或总额拒绝并提示分开核对。重复送审返回 200 和原草稿 ID，不重复建行或采购。一次最多 20 个内容。组内次级建议可读 `GET /api/intake-batches/<id>/review-suggestions?purchase_date=2026-11-08&source_name=Costco`（`limit` / `offset`，最多 200）：纸面单号相同仅是建议，绝不自动合成一张单据。

**单据边界只认纸面单号**（第 12 节）：`POST .../reviews/selection` 把单号相同的页合成一张草稿，**读不出单号的页一页一张**——同一个文件里挨着并不能证明是同一张发票，一份 PDF 本来就是几十张独立发票扫在一起的。合错了两张发票的金额会相加，而「各行之和对不上单据总额」这个检查正好因此失效；拆错了在填表页一眼看得见，用 `POST .../reviews/<invoice_id>/reassign` 并回去就是。表格行不走这条规则：一张 `entry_source="import"` 的草稿是整份表格的审核容器，按行拆开会把一份 CSV 变成几十张假发票。

表格行创建 `entry_source=import` 的导入草稿，图片/PDF 页创建 `entry_source=invoice` 的单据草稿；两类内容分别送审，保留各自现有审核和原件展示方式。

审核期间要拆页或并页，使用 `POST /api/intake-batches/<id>/reviews/<invoice_id>/reassign`，请求 `{"items":[{"id":1,"version":3}]}` 建新草稿，或另给 `target_invoice_id` 并入现有草稿；仅未处理的草稿可移动，已确认采购须先走原有撤销/修订流程。拆并后单据总额清空，需逐张对照原件重新填写，不会把组金额当单据总额。`GET /api/invoices/<id>` 的 `intake_sources` 返回批次、内容、页/行号、版本及文件名；原件读 `GET /api/intake-batches/<batch_id>/items/<item_id>/original`，PDF 响应为整份原文件，按页号在前端跳页。送审后不能从批次撤掉含该内容的原文件。

### 整条链路与送审状态（M12）

`GET /api/intake-batches/<id>/items` 与 `.../groups/items` 的每个内容上多一个 **`handed_off_at`**：不是 null 就表示这一项已经被 M08 送进审核，整理命令（`/moves`、撤销）会拒成 409，改日期/商家要走审核里的 `PATCH .../reviews/<invoice_id>/classification`。它存在的理由只有一个：**界面否则只能等 409 才知道这件事**，卡片在送审后看起来仍然可以拖。M12 之前这个字段只在数据库里。

整条链路是 **混合上传 → 整理 → 送审 → 填写 → 确认 → 收口 → 价格历史**，每一段都是已有接口，M12 没有新增业务规则：

```text
POST /api/intake-batches              建批次
POST .../files  /  .../spreadsheets   逐文件上传（图片/PDF 按页，CSV/XLSX 按行）
POST .../recognize                    日期/商家候选（文字层 0 次调用）
POST .../moves                        人工归组（日期 + 商家）
POST .../reviews                      明确选中的内容 → 一张待审核草稿
POST /api/invoices/<id>/lines         页上读不出明细行时手工加一行
PATCH /api/invoices/<id>/lines/<id>   填写
POST /api/invoices/<id>/confirm       逐行确认（这一下才产生采购记录）
POST /api/invoices/<id>/complete      整单收口
GET  /api/prices/food-items/<id>/history   价格历史回查
```

⚠️ **一个日期/商家组可能装着几张真正的发票**，所以送审送的是**明确选中的那几项**，不是「整组」。同组两张发票各自成为一张草稿、各自收口、各自入账；价格历史里比较的来源名与日期就是整理页上人工确认的那两项。

同一天的使用反馈（第 11 节）再加一条 **`POST /api/intake-batches/<id>/reviews/withdraw`**，请求 `{"items":[{"id":1,"version":3}]}`：撤回填表，把草稿连同填过的行一起删掉，内容的 `handed_off_at` 清空而 `decided_*` **保留**——撤的是「开始填表」这一步，不是分类。⚠️ 撤的单位是**整张草稿**（一张草稿就是一张发票），所以返回的 `item_ids` 可能比传进去的多；草稿里只要有一行已确认入账、单据已完成、或还挂着识别任务，就拒成 409，那条路仍是单据里的撤销确认与作废。`/items` 与 `.../groups/items` 的内容另外透出 **`review_invoice_id`**（在填哪一张草稿），整理页据此给出「继续填表」的入口；没有它，退出填表页之后卡片上就只剩一个 `handed_off_at` 标记而没有回去的路。没有迁移。

2026-11-24 的使用反馈（第 20 节）再加一条 **`POST /api/intake-batches/<id>/reviews/refill`**，请求与撤回同形 `{"items":[{"id":1,"version":3}]}`：**重新填写**——清掉这张单据填过的数据，并用同一批页立刻建一张新的空草稿（走的就是 `handoff`，识别结果照样预填），返回 `invoice_id`（新草稿）、`previous_invoice_id`、`previous_invoice_kept`、`cleared_lines` 与 `voided_purchases`。⚠️ 与 `withdraw` 的差别只有两处：①**已完成的单据走这条路**，它已经入账的行在同一个事务里按现有的软删 + 修改日志作废（不撤就会重复记价），所以 `withdraw` 那条「已入账不许撤」的 409 在这里是**撤销**而不是拦住；②清完立刻落进新草稿。旧草稿的去留沿用 `withdraw` 的规则：行上挂过采购记录就整张作废留着（那是价格历史的凭据），一条都没有就真删。`decided_*` 一个字不动。一次只能重填**一张**单据；补传过原件的单据拒成 409（`invoice_files.sha256` 全局唯一，留着作废会让同一张照片再也传不进新草稿）。没有迁移。

2026-11-23 的使用反馈增加了 `POST /api/intake-batches/<id>/reviews/selection`：同一天、同一商家的所选内容可一次开始填表；纸面单号不同的页在一个事务中拆成独立草稿，返回 `invoice_ids` 供前端切换，原有 `/reviews` 仍用于明确的一张草稿。日期类别由 `intake_date_groups` 单独保留空标题，使用 `POST /date-groups` 新建、`DELETE /date-groups/<YYYY-MM-DD>` 删除空类别；内容归属仍由内容本身派生。`clear` 移动通过既有 `decided_at` 记下人工清空，防止系统建议马上把它投影回原组。迁移版本为 `a8c92e74d130`，详见 [docs/multifiles.md 第 10 节](docs/multifiles.md)。

⚠️ **草稿可能一行都没有**。页上的文字层或识别结果没有可解析的明细行时，草稿就是 0 行——这是诚实而不是失败，填写从「手工加一行」开始。系统不会为了让草稿"看起来完整"而编出几行。

真实识别器（`OCR_PROVIDER=dashscope`）下这条链路也实测过：3 份真实原件 → 7 个内容单元 → `/recognize` 花 **4 次调用**（两张扫描页各 2 个模型，数字 PDF 走文字层 0 次）→ 再驱动一次 **0 次调用** → 卡片上读出纸面商家与日期 → 送审 → 草稿行与纸面行数相同 → 逐字段核对确认 → 收口（对账差额 0.00）→ 价格历史查到这笔采购。全量真实识别的分层结果（83 个单元、156 次调用、0 失败）与打分口径见 `backend/tools/eval_multifiles_m12_live.py` 和 `docs/multifiles.md` 的 M12 验证记录。⚠️ 准确率只来自仓库里那 10 个有逐字段人工真值的单元；手写 68 页只有覆盖与一致性。

真实原件的路径覆盖用 `backend/tools/eval_multifiles_m12.py` 复查（离线、不调模型）：它逐份报出 `samples/real/` 里每个原件走的是数字 PDF 文字层还是整页送识别器，并把那份数字 PDF 的日期/商家候选与人工转录的纸面值对照。⚠️ 仓库里的 `1.png`—`4.jpg` 与 `receipts/06..09` 是**同一批字节**（sha256 相同），不是八张不同的小票。

组分类要在审核中修改时，`PATCH /api/intake-batches/<id>/reviews/<invoice_id>/classification` 须带 `{"scope":"invoice","items":[{"id":1,"version":3}],"purchase_date":"2026-11-09","source_name":"Market"}`，`items` 必须列出这张草稿的所有内容。一次事务同步卡片、草稿头和**未设置行级覆盖、未单独修正过的**已入账采购，采购修改保留 `purchase_revisions`；行级例外仍保留。批次草稿直接 `PATCH /api/invoices/<id>` 改日期/商家会返回 409，避免只改抬头而让整理组和价格历史分叉；其它抬头字段仍用旧接口。批次草稿不能重新调用旧提取任务：M05 保存的候选轮次证据复用于草稿，手写页未经结构/墨迹审核的值只保留在 `raw_fields`，不自动预填数字；表格行复用原解析值与问题清单。确认、收口、采购修订与旧单张入口仍沿用原接口。

### 整理批次的日期/商家候选与日期解析（M05）

页与表格行切出来之后，整理页上的卡片还缺两样东西：**这是哪一天的采购、是哪家商家**。M05 为它们生成**候选**——⚠️ 候选**永远不会**变成 `decided_*`，界面必须把「系统建议」与「人确认过」分开显示。

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| POST | `/api/intake-batches/<id>/recognize` | 驱动识别队列；`{"retry_failed": true}` 连失败的一起重试 |
| POST | `/api/intake-batches/<id>/items/<item_id>/reparse-date` | 用保存的日期原文按**已确认商家**的 `date_order` 重新解析 |

`GET .../items` 的每张卡片多一个 `recognition` 块：`status`（`null` 还没读 / `pending` 排队中 / `done` 读完了 / `failed` 读失败）、`provider`、`models`、`prompt_sha12`、`date_raw_text`、`date_ambiguous`、`date_readings`、`date_order`、`notes`、`error`。

**分流按文件结构，不按商家，所以成本差一个数量级：**

| 内容 | 路径 | 模型调用 |
| --- | --- | --- |
| PDF 的某一页，**有文字层** | 抽文字层，**根本不调 OCR** | 0 |
| PDF 的某一页，没有文字层 | 取整页图 → 识别器 | 每个模型一次 |
| 图片（JPG / PNG / HEIC） | 解码摆正 → 识别器 | 每个模型一次 |
| CSV / XLSX 的某一行（M04） | 读那一行已经解析好的单元格 | 0 |

⚠️ **`status` 与「有没有候选」不是同一件事。** 读完发现纸面上没有商家时是 `done` + 0 条候选；没读过时两者都是空。分不开这两者，重新打开批次就只能把所有没候选的页再送一遍模型——那是对同一页重复收费。响应里的 `model_calls` 是**这一次真的发了几次调用**，重复驱动队列时它是 0。

⚠️ **商家没人工确认之前，不套用任何一家的 `date_order`。** 但 `6/11/2026` 这类写法**不会**因此停在「两种读法」上：依据从硬到软是「文本自己定死的 → 这家确认过的 `date_order` → 同文件同商家的明确证据（第 8 节）→ 本项目默认的月/日/年（`DATE_ORDER_ASSUMED`，[docs/multifiles.md 第 15 节](docs/multifiles.md)）」，所以它给出**一条**候选 6 月 11 日，响应里的 `date_ambiguous` 仍是 `true`、`date_assumed_order` 是 `mdy`——「文本定不死」是留痕的事实，不是要用户回答的问题。⚠️ 默认值只在**两种读法都成立**时出场：`18/6/2026` 本来就只有一种读法，把默认值当 `date_order=mdy` 一路传下去反而会把它整个丢掉。确认商家、并在 `PUT /api/ocr-routes` 上给这家设了 `date_order` 之后，`POST .../items/<id>/reparse-date` 用**保存下来的原文**重新解析，**不会再调一次模型**。拿相似度猜出来的商家去定一个日历日，猜错不报错——它只是把这笔采购算进了错误的月份。

**读不出来不是错误**，是这张卡片的状态：取不到字节、PDF 读不开、取不出整页图、模型调用失败，都写成 `failed` + 原因（`bytes_missing` / `pdf_unreadable` / `image_unreadable` / `page_image_missing` / `content_unsupported` / `ocr_failed` / `row_data_missing`），**原件一个字节都不动**，同一份 PDF 的其它页照常；重试要显式 `retry_failed=true`——否则每次驱动队列都会把钱花在同一个坏页上。

**来源不明就是不明。** 纸面读不出商家（或两个模型读法不一致）时**不给商家候选**，卡片留在「商家待定」。⚠️ 实测真实数字 PDF：卖方名是页首一行**没有标签**的标题，而页上唯一带标签的一方是 `Bill to:` 的**买方**——所以这里不会退而取页首标题或某个带标签的名字。跨批次复用确认过的来源名是 M07 的商家记忆。

### 商家记忆与相似堆（M07）

M05 把第一层线索（**纸面直接读出**）做完了。M07 做剩下三层，而且**一次模型调用都不发生**——建议服务结构上没有识别器这个依赖，三层线索全是本地计算与数据库查询。

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| POST | `/api/intake-batches/<id>/suggest-sources?limit=50&offset=0` | 重算第 2—4 层建议；`model_calls` 恒为 0 |
| GET | `/api/intake-batches/<id>/piles` | 相似外观的待办堆（**只读**，不量新版式） |
| GET | `/api/source-memory?limit=50` | 人确认过的来源 + 各自的别名（跨批次） |
| GET | `/api/source-memory/aliases?limit=50&offset=0` | 别名列表 |
| POST | `/api/source-memory/aliases` | 记下或**更正**一条别名：`{"alias_text":"COSTCO WHOLESALE #1162","source_name":"Costco"}` |
| DELETE | `/api/source-memory/aliases/<alias_id>` | 撤销一条别名 |

| 层 | `basis` | 依据 | 排序档位 |
| --- | --- | --- | --- |
| 2 | `same_file` | 同一份文件里别的页已经被人确认过商家 | 0.80 |
| 3 | `history` | 规范化后与确认过的来源相同（`history_exact`）、别名表记着这种写法（`history_alias`）、写法相似（`history_similar`） | 0.75 / 0.70 / 0.55—0.65 |
| 4 | `similar_layout` | 版式指纹相同，而同一指纹下已经有人确认过商家 | 0.40 |

⚠️ **档位只用于排序，不是概率、也不是阈值**：代码里没有任何一处拿它与某个线比较后自动确认。唯一的真阈值是步骤 10 的候选入选线（0.70），它只决定「这条相似写法值不值得摆出来给人看」。

⚠️ **纸面明确冲突的来源优先。** 这一页纸上读出的是 B、而同文件别的页确认成了 A 时，**不给这一页 A 的建议**；被压住的那条进响应的 `conflicts`，让界面能说出「单页证据优先」而不是静默不提。有纸面读法的页也**不进相似堆**——它已经有名字可接受，不需要靠外观。

⚠️ **版式相似不证明商家相同。** 同一指纹下有两家被确认过时，两家**同时**作为候选摆出来，系统不替人选；量不出格线的页（照片、空白页）标成 `layout_unknown`，那样的堆**一条商家候选都不给**——两张都量不出格线的歪照片「指纹相同」不是证据。

**整堆命名走 M06 那一条 `/moves` 命令**：`{"action":"source","items":[...],"source_name":"Kunkee Trading"}` 只写来源、**保留日期**（堆里的卡片常常各属不同的一天）。排除例外仍是 `action: "exclude"`。

**版式指纹落在内容上**（`intake_items.layout_fingerprint` / `layout_features`），由 `documents/layout.py` 本地量：有文字层的 PDF 取页面尺寸 + 字号集合，扫描页与图片取笔迹风格 + 格线行数 + 有无竖线 + 长宽比。一页约 0.3—0.6 秒，所以**指纹本身就是队列**：一次最多量 20 页，`layout_pending` 不为 0 时再调一次接着量，已量过的不会重量。⚠️ 已经确认过商家的页也会被量——它们是「历史版式」这条线索本身；真实实跑里正是漏了这一类，导致最常见的那条路径（纸面读出来就直接确认）攒不出任何版式记忆。

**记忆里只有别名落表。** 「这一家现在叫什么」取最近一次人工确认的写法（整理阶段的决定 + 已入账采购记录），「这个版式属于哪一家」由指纹加那一页上的确认派生。所以要更正版式记忆，改那一页的确认即可。别名**只影响建议**：它不会合并两家的采购记录，也不会改任何 `decided_*`。人手工设过的别名（`basis=user`）不会被自动重算覆盖。

### 真实 OCR 提取（步骤 08）

当前审核使用整页原件与识别原文。发票格子裁剪接口已移除；下文早期评测中提到的裁图能力仅是历史记录。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/invoices/<id>/extraction` | 这张单据的提取任务（最近的在前）：状态、分流结果、用了哪几个模型、提示词 sha、产出几行、失败原因与全部提示 |
| POST | `/api/invoices/<id>/extraction` | 重新提取。202 返回新任务 |
| GET | `/api/invoices/<id>/supplier-candidates` | **这家供应商历史上买过的食材**，最近使用优先。可用 `?source=` 覆盖单据头上的来源 |
| GET / PUT | `/api/ocr-routes` | 供应商 → 提取路径（`digital_pdf` / `printed_scan` / `handwritten_scan`），以及这家的 `date_order` |

几件要紧的事：

- **上传之后提取是异步的**（`OCR_WORKER_MODE=thread`）：`POST /api/invoices` 立刻返回，草稿行稍后由 worker 填进来，期间 `GET .../extraction` 能看到 `queued` / `running`。本地即时的路径（数字 PDF 文字层、Fake 识别器）会在当前请求里跑完——**三条路径的耗时差一个数量级，不该让最容易的单据陪最难的一起等**。
- **重新提取有两道闸门**：已有任务在跑 → 409（同一页图调两遍是白花钱）；已有行确认入账 → 409 并提示先撤销（重新提取会替换草稿行，和已入账的行对不上，而那种错位不报错）。**人改过但还没确认的行会保留。**
- **对照原件审核**：用 `/api/invoices/<id>/files/<file_id>/content` 回看整页；行号与墨迹结论仍保留在 `raw_fields` 中供核对。
- **候选分两种来源**：`/api/invoices/match` 按**原始描述**命中（手工、表格、印刷件有效）；`/api/invoices/<id>/supplier-candidates` 按**供应商维度**（手写件用这条——中文商品名跨模型分歧率 96.0%，模型给的名字几乎一定是错的）。
- **步骤 10 起每个候选都带 `match_reason` + `score` + `matched_description`**：
  `description_exact`（这句描述你以前就确认成它）/ `description_fuzzy`（有一句**像**
  的描述曾经确认成它，`matched_description` 说明是哪一句）/ `supplier_history`
  （这家供应商买过它，⚠️ `score` 是 `null`——它不是按文本比出来的）。
  ⚠️ 描述**精确命中过就不掺模糊结果**：确定答案旁边放几条"像的"只是噪声。
- **步骤 10 起非手写件的行会自动认食材**，痕迹在行上的 `food_item_match`：
  `description_exact`（老板确认过的描述又出现了，直接选中）/ `keyword_exact`
  （关键词命中食材库）/ `keyword_created`（按关键词自动建了一条，标 `needs_review`）
  / `needs_base_unit`（关键词剥出来了但**单位归一不出来所以没建**，带着
  `suggested_name` / `suggested_name_zh` / `suggested_category_id` / `unit_raw`，
  界面据此预填新建表单）。⚠️ **仍然不替老板确认任何一行**（与 05.6 同口径）。
  ⚠️ 闸门就是 08.3h 的那一条：**手写件一律不碰**。
- **收口对账多三项**（步骤 10.A）：`by_category`（按分类摊开本单金额，从大到小）、
  `adjustment_total`（**独立字段**，运费 / 折让那一桶）、`notes`。
  ⚠️ `adjustment` 行**不混进任何分类**，判据是 `line_kind` 而不是金额符号；
  多币种时 `by_category` 为空并在 `notes` 里说明原因。

```text
GET /api/invoices/12/extraction
  → {"items": [{"id": 7, "status": "succeeded", "doc_class": "handwritten_scan",
                "route_source": "supplier", "models": ["qwen3.5-ocr", "qwen3-vl-plus"],
                "prompt_sha12": "67fb7df1217b", "lines_created": 4,
                "warnings": ["本地笔画判别：…", "格线检测：去斜 -0.50°、26 条横线、行容量 25、5 个列带", …]}],
     "total": 1}

```

```text
POST /api/imports
  file=@nov-08.csv
  options={"sheet":"明细","header_row":1,"number_format":"decimal_dot","date_order":"mdy",
           "mapping":{"line_amount":5},"defaults":{"source_name":"Restaurant Depot","currency":"USD"},
           "invoice_number":"INV-77","invoice_total":"266.00"}
```

| 选项 | 作用 |
| --- | --- |
| `sheet` | XLSX 有多个工作表时必须指定（不替老板猜） |
| `header_row` | 表头在第几行（原始行号） |
| `mapping` | 按**列标题或列号**覆盖自动识别，传 `null` 表示忽略该字段 |
| `encoding` / `delimiter` | CSV 的编码（`utf-8-sig` / `utf-8` / `gb18030` / `utf-16`）与分隔符（`,` / `;` / Tab） |
| `number_format` | `strict`（默认）/ `decimal_dot` / `decimal_comma` |
| `date_order` | `strict`（默认）/ `mdy` / `dmy` |
| `defaults` | 整批共用的 `purchase_date` / `source_name` / `currency`；行上有值则行上的赢 |
| `invoice_number` / `invoice_total` | 整批的单号与总额（填总额时必须同时给 `defaults.currency`，否则总额币种不明） |

**列标题认中英文**：`商品描述` / `description` / `item`、`数量` / `qty` / `重量`、`单位` / `unit`、`单价` / `price`、`金额` / `小计` / `amount`、`采购日期` / `date`、`采购来源` / `供应商` / `supplier`、`币种`、`行类型`。至少要有商品描述列 + 数量 / 单价 / 金额中的一列；一个字段命中多列（重复表头）必须用列号指明。

**不猜的三处**（都会报错并给出 `sheet` / `row` / `column` / `cell`）：

- **逗号**：`1,234.5` 到底是一千二百多还是一点二三，默认不猜——选 `number_format`，或者在程序里填规范数值；
- **日月顺序**：`03/04/2026` 默认不猜——选 `date_order`；
- **编码**：CSV 解码失败就报错，不做替换字符式容错（那会把乱码当商品名存进去）。

解析不了的格子**留空**，原文仍然保留在行的 `raw_fields` 里（含工作表、行号、列号、单元格坐标、数字格式与公式原文）；公式单元格**不执行**，只带缓存值进草稿并挂一条「需人工复核」。

**格式与上限**：`.csv` 与 `.xlsx`；旧 `.xls` 明确拒绝（请另存）；含**合并单元格**的表拒绝导入；最多 5000 行 / 100 列、单元格 10000 字符、解压后 50 MB，单文件大小沿用 `UPLOAD_MAX_BYTES`。

**重传不重复入账**：整批写入是一个事务（任何一行出错整批回滚，刚存的文件也删掉）；同一个文件的 `sha256` 撞步骤 05 的唯一约束 → **409 并指回原批次**。改过的文件是另一份字节，会建新批次，但**不会**更新已经入账的旧记录——Excel 是输入格式，不是编辑器，这句话也写在导入响应的 `notice` 里。

**导出是明细。** 21 列：`purchase_id`、`line_kind`、食材 id / 英文名 / 中文名 / 分类、`purchase_date`、`source_name`、`original_description`、`quantity`、`purchase_unit`、`unit_price`、`line_amount`、`currency`、`entry_source`、`amount_mismatch`、`invoice_id` / `invoice_line_id` / `line_no`、`revision_count`、`voided_at`。默认只导未作废的采购行，**待确认的草稿行不在采购表里，自然不会混进价格明细**；对账用 `kind=all`。导出**不接受分页参数**（分页导出没法核对）。CSV 里以 `=` / `+` / `-` / `@` 开头的文本列加前导单引号、XLSX 一律写文本单元格——防的是导出文件被 Excel 当公式执行。

### 标准化价格与价格统计（步骤 09）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/prices/units` | 支持的单位、量纲与两条拒绝规则（静态数据，不查库） |
| GET | `/api/prices/food-items/<id>/history` | 采购历史，每行带标准化价格**或算不出来的原因**；分页 |
| GET | `/api/prices/food-items/<id>/monthly` | 月度加权均价序列 + 本期/基期比较。`?months=6`（1—36）、`?currency=` |
| GET | `/api/prices/food-items/<id>/weekly` | 周度加权均价序列 + 本周与上周同期比较。`?weeks=6`（1—104）、`?currency=` |
| GET | `/api/prices/food-items/<id>/sources` | 来源比价。默认窗口是最近 3 个月；`?period=week` 查看近 12 周，`?date_from=` / `?date_to=` 可改 |
| GET | `/api/prices/alerts` | 涨得超过阈值的食材。默认按月比较；`?period=week` 按周比较，`?threshold=15` 临时覆盖阈值（**不写库**） |
| GET / PUT | `/api/prices/alert-settings` | 提醒阈值与开关 |
| GET / POST | `/api/prices/pack-specs` | 箱规列表 / 新增或更新一条（同一个键是**更新**） |
| DELETE | `/api/prices/pack-specs/<id>` | 删一条箱规 |
| GET | `/api/prices/pack-specs/pending` | **待补箱规清单**，按涉及金额从大到小排，可能带候选 |

几件要紧的事：

- **标准化价格是查询时算的派生值**，`purchases` 上没有这一列。改箱规或改采购记录之后价格立刻跟着对——**包括历史**。所以 `POST /api/prices/pack-specs` 的响应带 `affected_purchase_count` 与一句 `notice`：「这条箱规影响 N 条采购记录的标准化价格（含历史记录）」。
- ⚠️ **算不出来是 `null`，不是 0。** `conversion.status` 四种取值与下一步该做什么：

  | 状态 | 含义 | 怎么解决 |
  | --- | --- | --- |
  | `available` | 算出来了，`normalized_unit_price` 是每个基础单位多少钱 | — |
  | `pending_pack_spec` | 按箱/包/罐/杯计价、还没有箱规 | `POST /api/prices/pack-specs` |
  | `unit_unknown` | 单位既不是计量单位也不是包装单位（`件/2`） | `PATCH /api/purchases/<id>` 改单位 |
  | `incompatible_unit` | 量纲不同（体积 vs 重量），没有密度不能换 | 改食材基础单位，或按实际称重录入 |

- **每个统计响应都带 `excluded` 与 `notes`。** 前者是「有几条没算进来、分别为什么」（`undated` / `unit_unknown` / `pending_pack_spec` / `incompatible_unit` / `other_currency`），后者是必须跟着数字一起读的话：不含运费与整单折让、不混币种、两个期间各多少天。`purchase_ids` 是追溯线索——**均价、涨幅和提醒都能回到参与计算的那几条采购记录**。
- **期间口径**：本月没过完时比的是「本月截至今天」vs「上月同样天数」，`basis` 为 `month_to_date`；上月天数不够（3/31 的上月是 2 月）就取到上月最后一天，两个期间的 `days` 都在响应里。⚠️ **缺基期不显示 0% 变化**：`change_percent` 是 `null`，`status` 分 `no_base_data` / `no_current_data` / `base_price_is_zero`（上月全是赠品，涨幅没有定义）。
- **币种不混算**：加总类接口（`monthly` / `sources`）遇到两种币种会报 400 并在 `details.available` 里列出可选项；逐条清单（`history`）可以混着列但标出 `available_currencies`；`alerts` 按（食材, 币种）分组，所以多币种既不报错也不相加。
- **箱规的键是（食材, 来源, 包装单位）**。`source_name` 留空表示**不限来源**，查找时**精确来源优先**。⚠️ 顺序反了就会用通用箱规去算某家的货——真实数据里那是 30 lb vs 44.1 lb，差 47% 且不报错。
- ⚠️ **待补箱规清单里的 `suggestion` 必须人确认**（响应里带 `confirmation_required: true`）：真实单据上 `1000g x 10` 是「每箱 10×1000g」，而 `6X30` 是「**6 箱** × 每箱 30 磅」，差 6 倍——所以裸数字写法一律不给候选。

```text
GET /api/prices/food-items/7/history
  → {"food_item": {...}, "base_unit": "lb", "currency": "USD",
     "items": [{"purchase": {...},
                "conversion": {"status": "available", "base_unit": "lb",
                               "base_quantity": "40", "normalized_unit_price": "5.50",
                               "pack_spec": {"pack_size": "40", "pack_unit": "lb", ...},
                               "pack_spec_match": "exact_source", "reason": null}}],
     "convertible_count": 1,
     "excluded": {"total": 0, "undated": 0, "unit_unknown": 0, "pending_pack_spec": 0,
                  "incompatible_unit": 0, "other_currency": 0},
     "notes": ["标准化价格只含商品行…", "不同币种分开统计…"]}

POST /api/prices/pack-specs
  {"food_item_id": 7, "source_name": "New Moy's Meat Market",
   "purchase_unit": "case", "pack_size": "44.1", "pack_unit": "lb",
   "spec_text": "6X44.10"}
  → 201 {"id": 3, "created": true, "affected_purchase_count": 12, "notice": "…"}

GET /api/prices/food-items/7/monthly?months=2
  → {"months": [{"period": {"key": "2026-10", "days": 31, "basis": "full_month"}, …},
                {"period": {"key": "2026-11", "days": 13, "basis": "month_to_date"}, …}],
     "comparison": {"status": "available", "change_amount": "1.00",
                    "change_percent": "20.00", "current": {…}, "base": {…}}, …}
```

## 8. 日志

- 格式：`2026-11-09 22:26:56,207 INFO [src.factory] GET /health -> 200`，按 UTF-8 写 stderr。Windows 控制台默认编码不是 UTF-8，中文路径与中文消息若直接写出会乱码，因此日志流由本项目显式包装。
- 内容：启动摘要（环境、日志级别、数据库、存储目录、OCR 适配器、监听地址）、访问日志（方法、路径、状态码）、错误日志。
- **不记录**：`SECRET_KEY`、连接串里的密码（自动替换为 `***`）、查询串与请求体。
- 级别由 `LOG_LEVEL` 控制。日志输出位置由启动入口决定（`configure_log_output()`），应用工厂只决定级别——这样测试进程不会在全局 logger 上累积处理器。

## 9. 配置变量

`.env.example` 是唯一交付的配置示例，`.env` 不进入源码交付。配置在启动时一次性解析并校验，不合法就不启动，不会带着错误配置运行。

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `APP_ENV` | `development` | 只接受 development / testing / production |
| `LOG_LEVEL` | `INFO` | DEBUG / INFO / WARNING / ERROR / CRITICAL |
| `SECRET_KEY` | `change-me` | production 下仍是占位值会直接启动失败 |
| `DATABASE_URL` | `sqlite:///var/inventory.db` | 相对的 SQLite 路径按 `backend/` 解析（见第 10 节）；应用与迁移共用 |
| `FILE_STORAGE_DIR` | `var/uploads` | 相对路径按 `backend/` 解析；目录在首次保存文件时创建 |
| `UPLOAD_MAX_BYTES` | `20971520`（20 MB） | 单个上传文件的上限；一次请求的总上限是它的 4 倍（超出由 Flask 返回 413）。小于 1024 直接启动失败 |
| `OCR_PROVIDER` | `fake` | `fake`（明确标识的假数据）或 `dashscope`（阿里云 Model Studio）。选了 `dashscope` 却没凭据会**启动失败**，不会静默退回假数据 |
| `OCR_API_KEY` | 空 | 真实适配器的 Key。兼容别名 `DASHSCOPE_API_KEY`（步骤 07 评测脚本用的那个），不必抄两遍 |
| `OCR_BASE_URL` | 空 | 工作空间专属域名。**故意没有默认值**：老域名自 2026-09-30 起不再支持新功能，猜一个的代价是"连上了但走的是将被弃用的那条路"，而且不报错。兼容别名 `DASHSCOPE_BASE_URL` |
| `OCR_MODELS` | `qwen3.5-ocr,qwen3-vl-plus` | 并行跑哪几个，第一个是主模型。步骤 07 落选的 `qwen-vl-ocr` 与未开通的 `vanchin/deepseek-ocr` 配上去会被直接拒绝并说明理由。⚠️ **改名单等于换一个被测配置**，准确率结论不再适用 |
| `OCR_TIMEOUT_SECONDS` | `240` | 单次调用超时。实测单页 5—7 秒、最慢 19.6 秒 |
| `OCR_MAX_ATTEMPTS` | `3` | 重试次数。只对限流与服务端故障生效；凭据无效不重试 |
| `OCR_WORKER_MODE` | `thread` | `thread`（应用内后台线程，提取**不挂在 HTTP 请求里**）或 `inline`（当前请求里跑完，测试与脚本用）。两者跑的是同一份提取代码 |
| `OCR_WORKER_THREADS` | `2` | worker 线程数 |
| `HOST` | `127.0.0.1` | `python app.py` 的监听地址 |
| `PORT` | `5000` | 必须是 1—65535 的整数 |

值为空或只有空白时按默认值处理。

⚠️ **密钥不进日志**：启动摘要里 `ocr_api_key_present` 只报「有没有」，连前几位都不给——日志会被复制进工单与聊天记录，而前缀足以泄露 Key 所属账号。endpoint 不是秘密，照原样打出来，因为「连到了哪个域名」是排查时第一个要看的东西。

## 10. 数据库、事务与迁移

### 事务边界由 Service 决定

[src/db.py](src/db.py) 提供 Engine、Session 工厂和事务上下文管理器。约定是：

```python
class PurchaseService:
    def __init__(self, database: Database) -> None:
        self._database = database

    def confirm_line(self, ...):
        with self._database.unit_of_work() as session:   # 事务边界在这里
            repository = PurchaseRepository(session)
            ...                      # 本次操作的多次写入共同提交或共同回滚
```

- Repository 只接收已有 Session 做读写，自己不提交、不回滚、不创建 Session；API 层不碰 Session。
- Service 之间互相调用时，把 Session 传给 `unit_of_work(session)`：内层只加入外层事务，不提前提交，因此不会出现「半个业务操作已入库」。
- 正常退出提交；异常回滚并原样抛出异常。提交本身失败时会尝试回滚，若回滚也失败，日志记录「结果未知」后抛出原始异常——不对外声称已经回滚干净。
- 步骤 05「写入失败不留下半条正式记录」依赖的就是这个结构。

### 启动不连数据库

`Database` 的 Engine 是懒创建的：创建应用只解析连接串，不连接数据库、不导入方言驱动。启动日志会如实记录这一点（`db_connected=False`）。

### 相对 SQLite 路径按 `backend/` 解析

`sqlite:///var/inventory.db` 在 SQLAlchemy 里是相对**当前工作目录**的，于是从不同目录执行 `python app.py` 和 `python -m alembic upgrade head` 会操作两个不同的文件。本项目统一把这类相对路径改写成 `backend/` 下的绝对路径（`resolve_database_url()`），应用与迁移因此总是指向同一个数据库。

### SQLite 的两处显式设置

- **外键校验**：SQLite 默认不校验外键，必须每条连接执行 `PRAGMA foreign_keys=ON`，否则外键约束只是注释。本项目在 Engine 的 connect 事件里统一打开，迁移也共用这套设置。
- **跨线程复用连接**：Flask 开发服务器是多线程的，连接池里的连接会跨线程复用，因此 SQLite 连接使用 `check_same_thread=False`（每个 Session 独占一条连接）。

### 金额与数量的精确存储（步骤 04 已解决）

三个数值列（`quantity`、`unit_price`、`line_amount`）按**整数最小单位**存，由 [src/models.py](src/models.py) 的 `DecimalAsInteger` 类型装饰器负责放大与缩小；数量与单价留 4 位小数、行金额留 2 位。进出都是精确整数运算，**全程不经 `float`**。

直接用 `Numeric` 被实测否决，三条都在本机验过：

1. 超出 scale 的位数被**静默**改掉——`Numeric(14, 4)` 收到 `12.34567` 存成 `12.3457`，没有报错也没有警告；
2. 有效位数超过 double 能承载的范围时回读就变了——`12345678901.234567` 变成 `…568`，`99999999999999.123456` 变成 `…125000`；
3. SQL 侧 `sum()` 是浮点运算（10 × `0.07` 得到 `0.7000000000000001`），而读取时按 scale 量化又会**掩盖**这个误差，于是错误不容易被发现。

存成 TEXT 能保住原值，但 `order by` 退化成字典序（`'10.1' < '100.0' < '9.9'`）、`sum()` 又被 SQLite 强转回 real。整数最小单位同时满足精确、可排序、可聚合，代价是 scale 固定在 schema 里（以后要放宽得靠迁移乘系数，所以一次定得宽些）。

配套的两条规则：**超出 scale 的输入拒绝而不是四舍五入**（舍入是无声地改数据），**API 响应里数值一律是字符串**（JSON 的数字是双精度浮点，前端 `JSON.parse` 会把 `1234567.89` 变成 double）。`tests/test_purchases_service.py` 里有 `typeof()` 断言，证明列在 SQLite 里确实是 INTEGER 而不是 REAL。

### 迁移

```bash
python -m alembic upgrade head          # 升到最新
python -m alembic current               # 查看当前版本
python -m alembic history               # 查看修订链
python -m alembic downgrade -1          # 回退一个修订
python -m alembic revision -m "说明"    # 新增修订
python -m alembic revision --autogenerate -m "说明"
python -m alembic -x db_url=sqlite:///tmp.db upgrade head   # 临时数据库
```

- 连接串**不写在 `alembic.ini`**：由 [migrations/env.py](migrations/env.py) 从 `DATABASE_URL`（`.env` 或进程环境变量）读取，优先级为 `-x db_url=` > `alembic.ini` 的 `sqlalchemy.url`（本项目留空）> `DATABASE_URL`。把真实连接串写进 ini 会连带密码进入源码交付。
- `render_as_batch=True`：SQLite 不支持大多数 `ALTER`，改约束要靠批处理模式重建表，而重建依赖约束有稳定名字——命名约定定义在 [src/models.py](src/models.py)，从基线修订起生效。
- **`alembic.ini` 必须是纯 ASCII。** Alembic 用 `encoding="locale"` 读这个文件，非 ASCII 内容在非 UTF-8 的本地环境（本项目开发机是 GBK）会直接 `UnicodeDecodeError`，所有 alembic 命令都无法执行。中文说明放在 `migrations/README`、`migrations/env.py` 与本文档；`test_migrations.py` 会在该文件混入非 ASCII 时失败。
- 修订链目前是五条：**基线修订**（只建 `alembic_version`，不建业务表）→ **建表修订**（步骤 03 的 `categories` 与 `food_items`）→ **初始分类修订**（写入 7 个常见分类）→ **采购修订**（步骤 04 的 `purchases` 与 `purchase_revisions`）→ **调整行修订**（`line_kind`，并把 `food_item_id` 放宽为可空）。发票与行项目在步骤 05 才加表，现在凭想象建表到时必然返工。
- **SQLite 批处理改表期间外键是关掉的，迁移后恢复并校验。** 批处理模式的实际做法是「建新表—搬数据—DROP 旧表—改名」，而 DROP 一张**被别的表引用**的表在外键打开时必然失败——`purchases` 被 `purchase_revisions` 引用，78a4af6ad755 改 `purchases.food_item_id` 的可空性时就撞上了 `FOREIGN KEY constraint failed`。SQLite 官方文档对这套改表流程的要求也正是先 `PRAGMA foreign_keys=OFF`。关掉不等于不管：[migrations/env.py](migrations/env.py) 在迁移结束后重新打开，并用 `PRAGMA foreign_key_check` 实际核对一遍，有违规就让 `alembic upgrade` 失败。两个辅助函数（`set_sqlite_foreign_keys` / `check_sqlite_foreign_keys`）放在 [src/db.py](src/db.py)，因为 `env.py` 是 alembic 的环境脚本、一导入就会执行迁移，放在那里测不了。
- **`PRAGMA foreign_keys` 在真有事务时是静默无效的**，所以 `set_sqlite_foreign_keys()` 设完会把值**读回来确认**。这里不能用 `Connection.in_transaction()` 判断：SQLAlchemy 2.x 会 autobegin 一个「逻辑」事务，连接一拿到手它就返回 True，但 pysqlite 要等到第一条 DML 才真正发 `BEGIN`——那个值与 PRAGMA 生不生效无关。
- **给既有行回填的 `server_default` 用完要撤掉。** 78a4af6ad755 给 `line_kind` 加列时带了 `server_default="purchase"` 回填既有行，随后在第二个批处理块里把它去掉：留着会与模型（只有 Python 侧 `default`）产生漂移，而本项目开了 `compare_server_default=True`，`test_models_match_migrations` 会直接失败。
- 结构与初始数据**分成两条修订**：`script.py.mako` 的约定是「迁移只改结构」，需要初始数据时单独说明来源与可重复执行方式。初始分类修订因此只插缺的那几条，重复执行不会报唯一约束错误，`downgrade` 按名称删回去。
- 迁移脚本**不 import `src/models.py`**：它是结构的历史快照，模型以后改名或搬家时旧修订仍要能原样执行。因此时间列写 `sa.DateTime()` 而不是 `src.models.UtcDateTime`，数值列写 `sa.Integer()` 而不是 `src.models.DecimalAsInteger`（两者生成的 DDL 完全相同）。**`alembic revision --autogenerate` 会在这里写出 `src.models.*`，而且丢掉 `DecimalAsInteger` 的 `scale` 参数，必须手工改回 impl 类型**——照抄自动生成的结果会得到一个 import 不到模型就崩掉的修订。
- 模型与迁移是否一致由 `tests/test_migrations.py::test_models_match_migrations` 用 Alembic 的 `compare_metadata` 断言，改了模型忘记写迁移会直接失败。
- **多文件计划 M02 的修订 `a3c81d57e240` 只加表、不改表**：五张 `intake_*`（整理批次、按内容摘要一行一份的原件字节、批次对原件的引用、可归类内容、分类候选）。⚠️ **`invoices` 与 `invoice_files` 一个字都没动**——那边 `sha256` 全局唯一是步骤 05「完全相同的文件不许重复上传」的落点、`invoice_id` 非空意味着一份文件只属一张单据，而整理阶段要的恰好是「一份文件的不同页属于不同组」。放宽旧索引会让旧单张上传路径的重复拦截失效，所以两套表并存，跨路径的同摘要文件只**提示**不阻断。M02 当时没有保存成员的「日期/商家组」表：组是 `(实际采购日, 规范化来源)` 的派生结果；后续 `intake_date_groups` 仅保留可为空的日期标题，不存成员名单。
- **多文件计划 M03 没有迁移。** 图片/PDF 的摄入用的是 M02 已有的五张表：切页的待办状态就是 `intake_batch_files.status`（`pending` = 字节存好了、还没切），**没有第二张任务表**——另立一张会出现「任务表说跑完了、文件还是 pending」两份能互相矛盾的真相。⚠️ 与 `extraction_jobs` 的差别是**代价**：那边有 `running` 中间态、重跑要再花一次钱，所以需要独立任务表与「卡住任务退回排队」；本地切页两者都没有，进程被 kill 之后那一条仍是 `pending`，再驱动一次队列就接着切。

## 11. 原始文件存储

[src/integrations/file_storage.py](src/integrations/file_storage.py) 把原始图片与 PDF 存到 `FILE_STORAGE_DIR` 下，返回一个 `StoredFile`：

| 字段 | 用途 |
| --- | --- |
| `key` | 存储内的相对引用，形如 `2026/11/09/3f6c….jpg`。业务表保存它，不保存绝对路径 |
| `original_name` | 上传时的文件名（含中文），原样保留，**不参与路径拼接** |
| `size` / `sha256` | 字节数与内容摘要。摘要是重复上传判定的依据：相同即同一份文件，直接拒绝并指回已有单据（`invoice_files.sha256` 上有唯一约束） |
| `content_type` | 调用方声明的 MIME 类型；未声明就是 `None`，本模块不猜 |
| `stored_at` | 保存时间（UTC，带时区） |

三条规则：

1. **原始文件只写不改**：不覆盖、不压缩、不转格式，读回的字节与写入完全一致——它是人工复核的证据。同名文件不互相覆盖，各自拿到不同的 `key`。
2. **存储键由适配器生成**：日期分目录 + 随机文件名 + 经过净化的扩展名。用户传的文件名影响不到落盘位置（传 `../../etc/passwd.pdf` 也一样）。
3. **路径边界强制校验**：每次按键定位文件都确认解析后的真实路径仍在存储根目录之内，越界一律抛 `ValidationError`；校验发生在 `Path.resolve()` 之后，因此 `..`、绝对路径、盘符、UNC 路径与符号链接逃逸都被拦住。`exists()` 同样拒绝越界键，而不是静默返回 `False`。

目录在首次保存时创建，创建适配器不产生副作用。文件大小上限与类型白名单属于**步骤 05 的上传通路**（`services/invoices.py` 与配置项 `UPLOAD_MAX_BYTES`），这里只管存字节；多页 PDF 的拆页与归并属于步骤 08。换成对象存储时实现同一组方法即可，上层拿到的仍是 `key`。

## 12. 识别契约、Fake 识别器与真实适配器

[src/contracts.py](src/contracts.py) 定义与厂商无关的识别请求与结果，[src/integrations/ocr.py](src/integrations/ocr.py) 定义 `Recognizer` 接口、Fake 实现与**步骤 08 接入的真实适配器**。业务层只认契约，不认厂商。

契约守住五件事：

1. **保留原文**：每个字段都带 `raw_text`，原样保留，不清洗不格式化——人工复核对照的是原文。
2. **保留页码**：`FieldEvidence.page` 从 1 开始，多页单据能说清某一行来自第几页。
3. **保留可获得的区域证据**：厂商给了坐标就记（`BoundingBox`，比例或像素坐标），**没给就是 `None`，不编造坐标**。
4. **未识别不是 0**：字段状态分 `RECOGNIZED` / `UNREADABLE`（有内容但认不出）/ `MISSING`（单据上没有）。契约会拒绝「标为已识别却没有原文」与「认不出却带着原文」这两种会被当成空值使用的组合。数值解析（原文 → `Decimal`）、「数量 × 单价 = 行金额」校验与单位换算都**不在**识别层：分别属于步骤 04（精确数值）、步骤 05（行级校验）与步骤 09（换算）。识别层多猜一步，错误就会被当成事实存下来。
5. **假数据必须自报**：`provider="fake"` 的结果必须 `is_fake=True`，契约层强制校验。

Fake 识别器（`OCR_PROVIDER=fake`，默认值）：

- 不访问网络、不读取任何凭据，因此**没有 API Key 也能启动和跑测试**；
- 每条文本都带 `FAKE` 标记，`warnings` 里用中文明说这不是真实识别结果，原始响应里也带标记与输入摘要；
- 刻意留下一行「单价认不出来」、一个「有文字没坐标」的字段和一个「单据上没写币种」的缺失字段——步骤 05 的审核流程因此从一开始就面对这几种真实情况：那一行确认时会被拦住并提示缺 `unit_price`；币种证据仍标记缺失，但新单据默认使用 USD，用户可在审核时改成 CNY；日期与总额因为原文带着 `FAKE` 前缀而解析不出、字段留空但原文保留在 `raw_document_fields` 里；
- 配置成尚未实现的厂商时，配置解析阶段直接报错，`build_recognizer()` 还会再拦一次，不会静默退回假数据。

真实适配器（`OCR_PROVIDER=dashscope`，步骤 08）：

- `CrossCheckRecognizer` **并行**跑 `OCR_MODELS` 里的模型（生产配置 2 个），逐字段裁决：一致 → 预填并标注「多模型一致」；**分歧 → `UNREADABLE`，必须人填**。并行发请求，总耗时等于最慢的那个，不是相加。
- ⚠️ **提示词是被测配置的一部分**：M01 运行时 `CONTENT_PROMPT` 增加卖方英文、中文及纸面线索，SHA 为 `67fb7df1217b`。步骤 07 的旧评测与 `tools/ocr_batch.py` 历史提示词仍绑定 `12e0246b0377`，其准确率不可算作新配置成绩。每条提取任务继续记录实际 SHA；商家原文和模型分歧进入单据头原始证据，英文纸面名优先作为待核对来源候选。
- ⚠️ **空缺一律是空字符串，不是 JSON `null`**：步骤 07 的 234 份结果里三个模型**一次都没输出过 null**。按 `null` 判空会漏掉全部空缺，所以适配器把空字符串归成 `UNREADABLE`（要人填），而不是 `MISSING`（纸上没有）——两者分不开时选保守的那个。
- ⚠️ **媒体类型按真实格式给，绝不写死 `image/jpeg`**：写死会让 PNG 被按错格式解码，而那种错误的表现是"识别结果变差"，不是报错。HEIC 必须先经 `documents.files.decode_image` 转成 JPEG 才允许送出去。
- **解析失败是必须有的分支**：234 份里有 1 份怎么都解析不出 JSON。那时产出 0 行并把原始输出原样留在 `raw_response` 里，**不猜**。

**提取流程**（[src/services/extraction.py](src/services/extraction.py)）把上面这些串起来：分流 → 格线检测 → （手写件先问结构）→ 内容识别 → 行容量上限 → 规范化 → 草稿行。它**从不写采购记录**——唯一入口仍然是老板点确认，所以「提取重试不导致采购重复入账」在结构上就成立。

**后台 worker**（[src/worker.py](src/worker.py)）：`python -m src.worker` 独立跑，或由 `python app.py` 在 `OCR_WORKER_MODE=thread` 时顺带起一个。提取状态落在 `extraction_jobs` 表里，进程重启、浏览器关掉之后仍然查得到；卡在 `running` 的任务在 worker 启动时退回排队。

`GET /health` 不检查识别服务，也不检查数据库连通性：没做的检查不会出现在响应里。

## 13. 测试

`python -m pytest` 运行全部测试，不需要网络、数据库与任何 API Key。测试一律注入独立配置（见 [tests/conftest.py](tests/conftest.py)），不读取本机 `.env`，也不依赖进程环境变量。

| 文件 | 覆盖内容 |
| --- | --- |
| `test_baseline.py` | 工程基线：Python 版本下限、依赖声明与实际安装版本一致、骨架包可导入、忽略规则覆盖密钥与运行期数据、配置示例无真实密钥、交付物不引用旧项目 |
| `test_config.py` | 默认值、注入映射不读进程环境变量、相对/绝对存储路径、非法 APP_ENV / LOG_LEVEL / PORT / OCR_PROVIDER、production 拒绝占位密钥、`repr` 与日志摘要屏蔽密钥和连接串密码 |
| `test_factory.py` | 配置注入、应用之间互不影响、未注入时读环境变量且非法配置直接失败、启动日志不含密钥、访问日志不含查询串、日志 UTF-8 且重复配置幂等 |
| `test_health.py` | 健康检查返回值、不声称未做的检查、响应无密钥、响应体由纯函数构造（不依赖全局 request）、只注册 `/health` 一条路由 |
| `test_errors.py` | 五类业务异常的状态码与错误码、`details` 透传与缺省省略、未知路由、非法 JSON、错误 Content-Type、未预期异常返回通用 500 且堆栈只进日志、所有错误响应结构一致 |
| `test_entry.py` | 入口暴露 Flask 应用、使用进程配置、只注册 `/health`、不定义路由与业务代码、`app.run()` 只在 `__main__` 分支、入口行数不超过 40 行 |
| `test_db.py` | 连接串解析与相对路径改写、Engine 懒创建（启动不连库）、提交持久化、失败整体回滚、嵌套作用域共用一个事务且内层不提前提交、会话互不可见未提交数据、SQLite 外键真的生效 |
| `test_migrations.py` | 在新建的临时数据库上真实执行 `upgrade head` / `downgrade base`、版本号被记录、只建出 `alembic_version`、`-x db_url` 生效、相对路径按 `backend/` 而非当前目录解析、单一 head、`alembic.ini` 纯 ASCII 且不含连接串 |
| `test_file_storage.py` | 字节往返一致（含二进制与空文件）、元数据与中文原始文件名、同名不覆盖、扩展名净化、路径边界（`..` / 绝对路径 / 盘符 / UNC / 空字符 / 反斜杠 / 链接逃逸）在全部入口被拒、未知键 404、写入中断不留残片 |
| `test_purchases_service.py` | 数值精度（原值进原值出、底层是 INTEGER、重启后仍精确）、无效数值与非规范写法被拒、0 元行可存、行金额为权威与容差边界、单位与来源的归一、日期可空与待补清单、历史不覆盖、纠错与修改日志、软删、调整行（负数、可无食材、默认排除、对账算例） |
| `test_purchases_api.py` | 六个接口的状态码与字段形状、数值在 JSON 里是字符串、`amount_mismatch` 错误码与确认流程、`kind` 筛选、`source_invoice` 占位、作废返回 200、调整行的录入与对账 |
| `test_contracts.py` | 原文与页码保留、区域可缺省但不编造、未识别不是 0、状态与原文的组合约束、置信度范围、页码/行号/字段名一致性、契约对象不可改、假数据必须自报 |
| `test_ocr.py` | Fake 结果的标识与警告、每条文本带 `FAKE`、保留未识别字段与缺失字段、按配置选择实现、未实现厂商被拒、无任何 API Key 也能创建应用、创建应用与 `/health` 不发起任何网络连接 |
| `test_ingredients_service.py` | 名称规范化（大小写/全角/空白）、基础单位白名单与同义词、包装单位被单独拒绝、初始分类及其规范化与服务实现一致、分类增删改与「被引用不能删」、快速新增只需三个字段、重复名称与重复中文名 409 且指向已存在记录、中英文与规格命中同一条、排序与分页、通配符按字面处理、局部更新与清空可选字段、改名后检索列重算、重开数据库后数据仍在 |
| `test_invoices_service.py` | 上传与类型/大小白名单、HEIC 只存不识别、重复上传的两种口径、草稿不编造数值、原始证据不可变、行三态与乐观锁、逐行确认（两行确认一行、重复确认、缺值被拦、金额差额、当场新建食材且失败不留残留）、继承与覆盖、撤销确认与重新确认、行匹配（同来源优先、忽略作废与调整行）、对账四态与收口确认、作废守卫、列表筛选与分页 |
| `test_invoices_api.py` | multipart 上传与 413、数值是字符串且认不出的是 `null`、批量确认返回 200 并逐行给结果、`amount_mismatch` 错误码与确认流程、原件按字节取回、收口确认的消息、作废守卫、`source_invoice` 追溯、`/api/invoices/match` |
| `test_spreadsheets.py` | CSV / XLSX 读入边界（编码、引号跨行、合并单元格、XML 实体、损坏文件）、数值不经 float 且不猜逗号、日期顺序与 Excel 纪元、中文输入、公式只标记不执行、列映射与重复表头、预览坐标、整批回滚不留孤儿文件与食材、改过的文件不覆盖旧记录、导出明细与筛选、模板下载 |
| `test_ingredients_api.py` | 9 个接口的状态码与响应体形状、错误结构与 `details`、未知字段与空请求体被拒、非 JSON 与非对象请求体、路径 id 非整数 → 404、405、查询参数解析与默认值、中英文搜索返回同一条、时间戳带 UTC 偏移、中文不被转义、重启应用后数据保留 |
| `test_normalization.py` | 数值与日期规范化（步骤 06 与 08 共用一份口径）：无歧义写法照常解析、**歧义写法一律拒绝**（`1,234` 两种约定差 1000 倍）、全角转半角留痕、尾部单位在两条通路上的不同处理、显式格式选项仍然有效、`6/11/2026` 这类日/月顺序靠供应商路由说一次 |
| `test_documents.py` | 本地文档处理：按内容嗅探格式（不信扩展名）、媒体类型跟随真实格式、PDF 文字层与扫描页的区分、格线检测与**诚实未判定**、裁剪的比例坐标与留边、印刷/手写笔画判别与「判不出按保守路处理」、文字层的列映射（竖线表按字段序号而非字符偏移）、`标签: 值` 同格拆分、总额只认纸面标签、商品名含 `total` 不被当成汇总行、切不出列时**拒绝而不是猜** |
| `test_ocr_adapter.py` | 真实适配器（假 HTTP，不碰网络）：M01 提示词 SHA 为 `67fb7df1217b`；卖方双语选择、买方排除、无来源与模型分歧；空字符串归成需人填、`-` 保留原文、围栏与裸 JSON 解析、媒体类型、重试和交叉裁决 |
| `test_extraction_service.py` | 提取流程（桩件识别器）：印刷件预填、**手写件一律不预填且仍保留两个模型的原文**、手写件先问结构而印刷件不花这一轮、行容量上限挡住「填满固定结构」、结构回答可进一步收紧上限、`quantity: 0` 按可疑清空、失败任务留下原因且不丢单据、已确认行后拒绝重新提取、人改过的行不被重试抹掉、供应商路由覆盖本地判别、`date_order` 每家说一次 |
| `test_worker.py` | 后台 worker：`thread` 模式下上传请求里不调模型、worker 取任务并跑完、**占用过的任务不会被第二次取走**、卡在 `running` 的任务在启动时退回排队、新鲜的 running 不算卡住、重复执行被拒、空队列不是错误 |
| `test_extraction_api.py` | 提取接口：任务可查询（含 `prompt_sha12` 与 `warnings`）、可重新提取、已确认行后 409、裁剪必须显式指定格子、路由的增查与校验、供应商维度候选 |
| `test_matching.py` | 文本匹配原语（步骤 10）：⚠️ **分词必须在书写系统边界切开**（真实单据上 `Beef Stomach - 牛肚6X44.10` 中文名与箱规之间一个空格都没有，不切开则库里的 `牛肚` 永远对不上）、包装规格与单位字剥掉、**抽掉元音的缩写**（`cntry`/`country`、`brst`/`breast`，都来自真实样本）、⚠️ **短子序列不算缩写**（`ct` 不得命中 `condiment`，而 `Pork - CT` 在真实单据上出现 3 次）、中文按字二元组比（`牛肉` 与 `牛肚` 重合为 0）、单向 vs 双向相似度的分工 |
| `test_suggestions.py` | 候选与榜单（步骤 10）：四档命中的理由与排序（⚠️ **精确命中永远排在模糊命中之前**，哪怕模糊分更高）、模糊层只在精确层没填满时才扫库、「候选里没有」是确定答案、⚠️ **最近按最近一次入账排而不是纸面日期**（步骤 04 允许日期留空）、常用的 90 天窗口与**未填日期进不了榜**、调整行与已作废记录不算"用过"、打了字就不给榜单 |
| `test_merging.py` | 重复食材合并（步骤 10）：三处引用全部搬过去且**数值一字未改**、作废记录也搬（不搬会留悬空外键）、发票行 `version` 不动、相同箱规丢重复份、⚠️ **矛盾箱规与不同量纲的基础单位拒绝而不是挑一个**、**被拦住时什么都没动**、合并不可逆这句话固定出现、旧叫法仍可搜（中英文都验）、旧叫名被释放后可再建再合并（所以那一列不能唯一） |
| `test_keyword_items.py` | 关键词与分类（步骤 10）：15 条**真实商品描述**逐条断言关键词与猜出来的分类、⚠️ **7 个 NewMoys 食材只落 1 个类别**（这是「关键词落在食材上不落在类别上」那个决定的依据）、词表只返回待查的键**不碰数据库**、「未分类」锚点**不许删但随便改名**、`needs_review` 的置位与清除 |
| `test_units.py` | 单位换算（步骤 09）：转写自旧项目的三条算例、**同单位也校验输入**（`case → case` 报错，旧测试那条 identity 断言被刻意改掉）、美制体积制式钉死在定义值上、体积↔重量拒绝、除不尽时精度足够、拒绝 `float`、真实单据的单位写法（`lbs.` / `heads` / `gal.`）、**包装规格原文同形不同义一律不给候选**（`6X30` vs `1000g x 10` 差 6 倍）、不拿算术自洽当判据 |
| `test_prices_service.py` | 标准化价格与统计（步骤 09）：09.10 的三个验收算例、09.11 的四条拒绝（未知包装 / 待确认草稿 / 不可换算 / 缺基期不显示 0%）、调整行与已作废记录永不进均价、未填日期被数出来、0 元与金额异常行计入但报数、多币种不猜、箱规 upsert 与精确来源优先、待补箱规清单的候选只在无歧义时给出、期间口径（本月截至今天 vs 上月同样天数、上月短则截断）、来源比价与差价、提醒（阈值、关闭、临时覆盖、跌价不报、按币种分组、排序）、**统计触顶报错而不截断** |
| `test_prices_api.py` | 价格接口（步骤 09）：11 个路由的状态码与字段形状、数值是字符串、**算不出来是 `null` 不是 0**、多币种报 400 并带可选币种、箱规 upsert 返回 201/200 与受影响条数、删箱规后那些行退回"待补箱规"而采购记录不动、提醒设置往返与拒绝 JSON 小数、`/api/prices/forecast` 这类未实现路径是 404、**价格接口只读不写采购记录** |
| `test_intake_service.py` | 上传整理批次（多文件计划 M02）：**同一份 PDF 的第 1 页与第 3 页是两个独立内容、各自回指原页**、页可落到不同的日期/商家组、组由决定值派生（改一张卡片组立刻跟着变）、拖到日期标题不会抹掉已确认的商家、`Costco` 与 `COSTCO` 不分成两组、表格行保留原行号与工作表名、**原件字节只存一次**（两个批次共用一行 `intake_files`）、同批次重复文件 409、回收后再传同一文件连分类一起复活、回收要能说出字节是否还被引用、**旧发票照常回读且旧的全局重复上传拦截仍然有效**、切页失败整体回滚不留半套关系、页号/坐标/来源名成对三条 CHECK 在数据库上真的拦住、乐观锁拦住并发移动而重复同一命令不涨版本、歧义日期文本拒绝而不猜、**候选永远不会自己变成决定**、按依据整组替换候选、批次状态派生自两个时刻、大表读取分页 |
| `test_intake_ingest.py` | 图片/PDF 的批次摄入（多文件计划 M03，Service 层）：**一份五页 PDF 产生五个各自回指原页的内容**、一张图片恒等于一页、**按内容嗅探而不是按扩展名**（`.jpg` 里装着三页 PDF 就切三页）、超过页数上限整份失败不留半套、同一份字节在两个批次里只存一份、同批次重复 409 且不留孤儿字节、**被拒的上传一个字节都不留**、第 2 个文件失败不连累第 1 个、坏 PDF/坏图/非图非 PDF 各自带可读原因、**中断后重试真的切出页**（不是只改状态）、已切过的文件不重切（页的 id 稳定）、撤掉的文件不被偷偷切回来、**后台调度器把切页挪出调用**、交给 M05 的入参只有页坐标与本地事实（没有任何商家/日期字段）、处理器抛异常也不丢已登记的页、默认不挂处理器时一个候选都不产生、回看原件字节与原文件完全一致、跨路径同字节只提示不阻断 |
| `test_intake_api.py` | 整理批次的 HTTP 边界（M03）：建批次与「**没有在整理的批次是 404 而不是空对象**」、五页 PDF 的五个页引用逐个可回看且 `X-Intake-Page` 正确、**状态里没有百分比**、一次一个文件（字段 `file`，多传/传错字段都拒）、不受理的扩展名带受理清单、同批次重复 409 并指回已有那条、第 2 个失败时第 1 个与批次仍可用、**坏文件是 201 + `failed` 状态而不是 500**（且照样能回看原件）、`/process` 逐项汇报结果与 `retry_failed`、撤掉文件时说清字节是否还被引用、**换个批次号读不到别人的原件**（404）、旧单张上传 API 并存且其全局重复 409 仍有效 |
| `test_intake_recognition.py` | 整理批次的日期/商家候选（M05，Service 层）：**数字 PDF 走文字层、一次模型都不调**、一份 PDF 的两页各拿各自的商家与日期、行项目一起存下来给 M08 复用、**`6/11/2026` 留两条读法而商家读不出就是 0 条候选**、**候选永远不会变成决定**（`decided_*` 仍空、整页还在待归类）、纸面两种商家写法都留在证据里、纸上没有商家就不编一个、**读完没读到东西是 `done` 不是 `pending`**、⚠️ **这家的 `date_order` 已设好但商家没人工确认时日期仍然歧义**、确认商家后重解析得到唯一日期且**一次调用都没发生**、重解析不动商家候选与人工决定、没确认商家就重解析报错、没读过就重解析是冲突、确认了商家但这家没说过日/月顺序仍然不猜、**重复驱动队列第二次是空的且 0 次调用**、摄入钩子只排队不调模型、模型调用失败只标这一个内容而**原件照常可回看**且可重试、失败默认不自动重试、一页失败不连累同份 PDF 的别的页、字节不见了说清怎么办、撤掉的文件不被偷偷识别、表格行用**原单元格证据**且 0 次调用、表格里歧义日期同样留两读法、一行数据丢了只失败那一行、表格行与页共用同一条重解析路径、重算候选整组替换不叠加 |
| `test_intake_recognition_api.py` | M05 的 HTTP 边界：`/recognize` 返回逐项结果与**`model_calls`（文字层是 0）**、卡片上的 `recognition.status` 能分开「还没读 / 读完了 / 读失败」、歧义日期在卡片上就是两种可能且带原文、**再驱动一次是空结果 0 次调用**、读不出来是 **200 + 这一页的原因**而不是 500（且原件照常可回看）、`retry_failed` 要校验、未知批次 404、**没确认商家就重解析是 400**、确认商家后重解析得到唯一日期且 `model_calls=0`、**换个批次号动不了别人的卡片**（404） |

| `test_intake_suggestions.py` | 商家记忆与相似堆（M07，Service 层）：同文件已确认页给其它页建议、**纸面写着别家时同文件建议被压住并进 `conflicts`**、确认之后过期建议被清掉而纸面读法保留、`COSTCO` 收敛到人上次写的 `Costco`、**确认另一种写法会记下可更正的别名**且删掉别名建议立刻消失、人工别名不被自动重算覆盖、**同一张纸上的中英文两种写法互为别名（不是翻译）**、相似写法只排序且分数低于别名档、**没有纸面读法就一条历史建议都不给（也绝不看文件名）**、**六张一堆排除一张例外后一次给剩余五张赋来源且日期仍待定**、以后的相似单据出现候选且能纠正、**同版式下两家已确认时两家同时摆出来**、量不出格线的堆一条商家候选都不给、两个模板不共用一堆、**指纹就是队列（量过的不重量、没量完的下次接着量）**、量不出版式的页如实记下来不反复重量、表格行与已确认页不进堆、建议**结构上**花不起钱（服务里没有识别器）、别名入参校验 |
| `test_intake_suggestions_api.py` | M07 的 HTTP 边界：`/suggest-sources` 返回堆与逐卡片建议且 **`model_calls` 是 0**、`/piles` 只读并单独列出例外、**整堆命名走 M06 的 `/moves`（`action: source` 只写来源）**、`/api/source-memory` 列出确认过的来源与别名、别名可建可删、规范化后同名的别名是 400、未知批次 404、未知字段 400、**`source` 不接受日期** |
两点说明：

- `test_errors.py` 里的 `/__probe/*` 路由只在测试内注册，不随应用交付：它们用来验证异常映射这一层，而不是给未实现的业务提供可调用接口。
- `test_db.py` 里的 `tx_probe` / `tx_parent` / `tx_child` 三张表属于该测试模块自己的 `MetaData`，**不挂在 `src/models.py` 的 `Base` 上**：验证提交与回滚不需要、也不应该借用业务表。测试会断言这几张表与业务模型无交集，并断言业务模型里当前**恰好**是步骤 03—10 的十二张表加多文件计划 M02 的五张 `intake_*`、、M06 的两张操作表、M08 的审核关联表与 M07 的来源别名表，且金额与数量**不经 `Numeric` / `Float`**（防的是以后有人图省事改回 `Numeric`，那会把精度问题悄悄带回来）。
- **步骤 08 的测试一律不访问网络**：传输层用假响应注入、识别器用桩件、图像是程序生成的（真实单据含客户数据，不进源码交付）。真实接口与真实样本上的验证由 `tools/eval_adapter.py`（按 GT 计分）与 `tools/e2e_extraction.py`（整条链路跑全量）负责，结论记在 [docs/impl_record.md 步骤 08](docs/impl_record.md) 里——**那两个脚本会花钱，所以不进 `pytest`**。步骤 09 的真实数据实跑是 `tools/e2e_prices.py`（结论记在 [docs/impl_record.md 步骤 09](docs/impl_record.md)）：⚠️ 它**默认一次模型调用也不发**（数字 PDF 走文字层、印刷件用人工 GT 值），只有 `--with-ocr` 才会真调模型。步骤 10 的真实数据实跑是
`tools/e2e_keywords.py`（结论记在 [docs/impl_record.md 步骤 10](docs/impl_record.md)）：
⚠️ 它**一定会真调模型**（关键词要从模型读出来的描述里剥，没有免调用的路径），
所以同样不进 `pytest`。
- 业务测试跑在**真实迁移建出来的数据库**上，不用 `Base.metadata.create_all()`：`conftest.py` 的 `migrated_template` 夹具在会话开始时执行一次 `alembic upgrade head`，各用例复制那个文件（`migrated_database` / `migrated_app` / `migrated_client`）。于是唯一约束、外键与初始分类都是真的在起作用，而「迁移能不能跑」也不会被绕过。

### 符号链接用例为什么改用目录联接

`test_link_escaping_the_root_is_rejected` 验证的是「根目录里有个链接指向外部时，`Path.resolve()` 之后的归属判断仍然拦得住」。它原先只用 `os.symlink`，在本机**长期被跳过**：Windows 建符号链接需要管理员特权或本地安全策略里的 `SeCreateSymbolicLinkPrivilege`，普通账户拿到 `WinError 1314`。

改法是**先试符号链接，失败就退回目录联接（junction，`mklink /J`）**。三条理由：

1. **junction 不需要任何特权**，而 symlink 需要管理员——本机实测确认了这个差别；
2. `Path.resolve()` 同样会跟随 junction，所以被测的那条防线照样被触发，用例没有被削弱；
3. 更关键的是**顺序反了**：在 Windows 上普通用户**建得出** junction、**建不出** symlink，所以 junction 才是真正可能发生的那种越界，原先的写法恰好跳过了唯一能实际发生的情形。

用例里多了一句前置断言（`root.resolve() not in escaped.parents`），确认链接真的指向根目录之外——否则「链接没生效」会让用例假装通过。另外做过一次变异验证：把 `_resolve()` 里的归属判断改成 `if False` 后该用例立刻 `DID NOT RAISE ValidationError`，证明它拦的就是那条防线，不是碰巧通过。

不需要特权就能跑，所以**整套测试现在是 0 skipped**；`test_containment_check_is_a_second_line_of_defence` 仍然保留，它从另一个角度（绕过键形状校验）覆盖同一条防线。

## 14. 实际验证记录

### 里程碑 A（步骤 00—02）：2026-11-09 在 Windows 11（win32）实测

四条通路逐条执行，命令与结果如下：

| 通路 | 命令 | 结果 |
| --- | --- | --- |
| 安装 | 新建独立虚拟环境后 `python -m pip install -r requirements-dev.txt` | 成功；`pip freeze` 与开发环境逐行一致（见下表） |
| 启动 | `python app.py` | 监听 `127.0.0.1:5000`；`GET /health` → 200 `{"status":"ok","app_env":"development","version":"0.1.0"}`；未知路由 → 404 `not_found`；`POST /health` → 405 `method_not_allowed` |
| 迁移 | `python -m alembic upgrade head` → `python -m alembic current` | 升级到 `73ebb18fcafe (head)`；库中只有 `alembic_version` 一张表，内容为该版本号 |
| 测试 | `python -m pytest` | **258 passed, 1 skipped** |

补充记录：

- **干净环境安装**：在另建的全新虚拟环境中安装 `requirements-dev.txt`，`pip freeze` 与开发环境的解析结果逐行一致；在该环境中 `python -m pytest` 同样 258 passed / 1 skipped，`python -m alembic -x db_url=... upgrade head` 同样升级成功。
- **唯一的跳过项**：`test_file_storage.py::test_symlink_escaping_the_root_is_rejected`——本机账户没有创建符号链接的权限（`WinError 1314`），用例按环境跳过而不是假装通过。同一层防护由不依赖权限的 `test_containment_check_is_a_second_line_of_defence` 覆盖（绕过键形状校验后，`Path.resolve()` 之后的归属判断仍然拦住越界）。
  **2026-11-10 已消除这个跳过**，改法见第 13 节「符号链接用例为什么改用目录联接」；当前整套测试是 538 passed, 0 skipped。上面的数字保留当时的实际运行结果，不回头改写。
- **启动行为**：启动日志记录 `db_connected=False`、`ocr=fake ocr_fake=True`——创建应用不连接数据库，也不连接任何识别服务。`test_ocr.py` 用禁止 `socket.connect` 的方式断言了这一点。
- **迁移与应用指向同一个数据库**：从其它工作目录执行迁移，相对的 `sqlite:///var/...` 仍解析到 `backend/var/`（`test_migrations.py::test_relative_sqlite_url_resolves_against_backend_not_cwd`）。
- **日志**：启动摘要与访问日志按 UTF-8 写出，中文路径正常；日志中不含 `SECRET_KEY` 的值，SQLite 路径按原样可读（不做百分号转义）。
- **尚未验证的事项**：真实 OCR（步骤 07—08）未执行，当前只有 Fake 通路；SQLite 下数值的精确存储（步骤 04）未实现也未测试；业务 Feature 全部未实现。

### 步骤 03（食材数据库、基础搜索与快速新增）：2026-11-10 在 Windows 11（win32）实测

| 通路 | 命令 | 结果 |
| --- | --- | --- |
| 测试 | `python -m pytest` | **423 passed, 1 skipped**（步骤 00—02 为 258 passed；新增 165 项） |
| 迁移 | `python -m alembic upgrade head` → `current` | 升级到 `c6f14af1ab06 (head)`；库中为 `alembic_version`、`categories`、`food_items`，分类表有 7 行初始数据 |
| 启动 | `python app.py` | 监听 `127.0.0.1:5000`；`/health` 仍为 200 且字段不变 |
| 接口 | 对运行中的进程发 24 项请求 | 全部符合预期，详见下面的要点 |

接口核对要点（请求体按 UTF-8 发送；本机终端是 GBK，用 `curl` 在命令行里直接写中文会在送出前被改写，那是终端的问题，不是接口的问题）：

- `GET /api/categories` 返回 7 个初始分类，带中文名，时间戳形如 `2026-11-10T02:30:47.461187+00:00`。
- 完整新增（含 `name_zh`、`specification`）→ 201，中文原样返回；只给 `name` / `category_id` / `base_unit` 的快速新增 → 201，可选字段为 `null`，`lbs` 归一为 `lb`。
- `q=牛肉末`、`q=ground beef`、`q=GROUND  BEEF`、`q=beef`、`q=80/20`、`q=牛肉` 六种写法都只命中同一条食材。
- 重复名称 `"  GROUND   beef "` → 409，`details.existing.id` 指向已存在的那一条；重复中文名同样 409。
- `base_unit: "case"` → 400，消息说明它是包装单位并列出可选的基础单位。
- 删除在用分类 → 409（`details.food_item_count = 2`）；删除无引用食材 → 204，再查 → 404。
- `PATCH {"base_unit": "kg"}` 只改该字段，`specification` 保持原值；`PATCH {"baseUnit": "kg"}` → 400（未知字段）。
- `GET /api/suppliers` → 404：未实现的业务不注册路由。
- **重启后数据保留**：停掉进程、重新 `python app.py`，之前写入的 `Ground Beef / 牛肉末 / 80/20` 与 7 个分类都还在。

尚未验证的事项（与步骤 03 无关，按原状保留）：真实 OCR（步骤 07—08）、发票上传与逐行确认（步骤 05）、CSV / Excel 导入导出（步骤 06）、单位换算与价格统计（步骤 09）。SQLite 下数值的精确存储已在步骤 04 解决并验收，见下一节。

### 步骤 04（采购记录、精确数值与手工录入）：2026-11-10 在 Windows 11（win32）实测

| 通路 | 命令 | 结果 |
| --- | --- | --- |
| 测试 | `python -m pytest` | **537 passed, 1 skipped**（步骤 00—03 为 427 passed；新增 110 项） |
| 迁移 | `python -m alembic upgrade head` → `current` | 从 `c6f14af1ab06` 升到 `520863805194 (head)`；库中为 `alembic_version`、`categories`、`food_items`、`purchases`、`purchase_revisions` |
| 模型与迁移一致 | Alembic `compare_metadata` | 差异为空（`test_models_match_migrations`） |
| 启动 | `python app.py` | 监听 `127.0.0.1:5000`；`/health` 仍为 200 且字段不变 |
| 接口 | 对运行中的进程发 30 项请求 | 30/30 符合预期，详见下面的要点 |

接口核对要点：

- **精度**：`0.07`、`1234567.89`、`12.345` 三个值原值进原值出；三个数值字段在 JSON 里都是字符串。直接查库确认 `typeof(quantity)` 等于 `integer`、值为 `123450`（即 12.345 × 10⁴），列在 SQLite 里是 INTEGER 而不是 REAL。
- **无效数值被拒**：负数单价、数量为 0、`"abc"`、`"1,234.56"`、`"12.34567"`（超小数位，**不四舍五入**）、JSON 小数 `12.345` 全部 400，且 `details.field` 指向出错的字段。
- **0 元与负数**：赠品行 `unit_price: "0"` → 201（输出 `"0.00"`）；负数 → 400 并说明退货/折让本轮不支持。
- **采购单位**：`"CS"` → 201，原文保留为 `CS`、`purchase_unit_normalized` 为 `case`（与食材的基础单位不同，`case` 可以当采购单位）；`"件/2"` → 201，归一结果为 `null` 且不阻断录入。
- **行金额为权威**：11.3 × $6.35 = $71.755 对 $71.76（差 $0.005）静默接受，`amount_mismatch` 为 `false`；3 × $10 对 $35 → 400 `amount_mismatch`，`details` 给出 `expected: "30.00"` / `difference: "5.00"` / `tolerance: "0.05"`；带 `accept_amount_mismatch: true` 重发 → 201，按 `"35.00"` 入账并标记。
- **日期可空**：不填 `purchase_date` → 201；`GET /api/purchases?undated=true` 能把它筛出来；列表里未填日期的排在最后。未来日期（今天 + 30 天）→ 400。
- **历史不覆盖**：同一食材 7 条采购全部在列，按 `purchase_date DESC` 排列。
- **纠错**：`PATCH {"unit_price": "11.6667", ...}` → 200；`GET /api/purchases/<id>/revisions` 返回 `unit_price: {before: "10.00", after: "11.6667"}`，并连带记下 `amount_mismatch: {before: "true", after: "false"}`（改完之后三个数字对上了，标记自动清掉）。`PATCH {"food_item_id": ...}` 可直接改匹配错的食材。
- **作废是软删**：`DELETE` → 200 `{"status": "voided"}`；记录仍能单条查到且 `voided_at` 有值，默认列表里看不到、`include_voided=true` 能查到；再次作废 → 409，修改已作废记录 → 409。
- **食材删除**：只有独立采购记录时连同修改历史一起删除；草稿中预选的食材会清空选择，保留单据；已确认入库或有单据来源的采购记录时 → 409。
- **重启后数据保留**：停掉进程、重新 `python app.py`，14 条采购记录、三个精确数值、2 条作废状态、4 条修改日志、2 条未填日期的记录都还在；另单独验证了一条 `amount_mismatch: true` 的记录在重启后仍为 `true`、行金额仍是 `"41.00"`。

尚未验证的事项：真实 OCR（步骤 07—08）未执行，当前只有 Fake 通路；发票上传与逐行确认（步骤 05）、CSV / Excel 导入导出（步骤 06）、单位换算与价格统计（步骤 09）均未实现。**不要把「发票能录进去」说成「价格分析可用」**：现在只能看到原始计价单位的价格。

### 步骤 04 追加：退货 / 折让等调整行（2026-11-10 在 Windows 11 实测）

| 通路 | 命令 | 结果 |
| --- | --- | --- |
| 测试 | `python -m pytest` | **575 passed, 0 skipped**（追加前为 538；新增 37 项） |
| 迁移 | `python -m alembic upgrade head` → `current` | 从 `520863805194` 升到 `78a4af6ad755 (head)`；既有 15 条采购行全部回填为 `line_kind='purchase'`，6 条修改日志在批处理重建表后仍在，`PRAGMA foreign_key_check` 无违规 |
| 回退 | `downgrade -1` → `upgrade head` | 可往返；回退会**删掉调整行**（旧结构表达不了它们），这一点写在修订文件开头 |
| 模型与迁移一致 | Alembic `compare_metadata` | 差异为空 |
| 接口 | 对运行中的进程发 18 项请求 | 18/18 符合预期 |

接口核对要点：

- 退货行 `quantity: "-2"` / `line_amount: "-12.00"` → 201，数值原样返回且 `amount_mismatch` 为 `false`（`-2 × 6.00 = -12.00` 自洽）。
- 整单折让（不填 `food_item_id`）→ 201，响应里 `food_item` 为 `null`。
- 正常采购行传 `line_amount: "-60.00"` → 400，`details.hint_line_kind` 为 `"adjustment"`（告诉人该怎么录，而不是只说不行）。
- 正常采购行不填 `food_item_id` → 400，`details.field` 为 `food_item_id`。
- 0 元调整行 → 400（`details.field` 为 `line_amount`）。
- 调整行 `-2 × $6.00` 对 `-$50.00` → 400 `amount_mismatch`（符号放宽了，算术校验没放宽）。
- `GET /api/purchases` 默认只返回采购行；`kind=adjustment` 只返回调整行；`kind=all` 三行相加 `60 - 12 - 20 = 28.00`，对账口径成立。
- 按食材查历史（`?food_item_id=`）默认不含退货行。
- `kind=refunds` → 400；`PATCH {"line_kind": ...}` → 400（未知字段）。
- 调整行可修改（`-12.00` → `-18.00`）且修改日志记下负数的前后值；可作废，作废后 `kind=all&include_voided=true` 仍能追溯。

这一步同时改了**迁移基础设施**：SQLite 批处理改表期间关闭外键、迁移后恢复并用 `PRAGMA foreign_key_check` 校验（原因见第 10 节）。配套加了 5 项回归测试，其中 `test_upgrade_rebuilds_purchases_even_when_rows_reference_it` 在「有引用行」的库上迁移——把那段逻辑去掉后它会以原始的 `FOREIGN KEY constraint failed` 失败（已做变异验证）。

仍然**不做**：库存调整、退款管理、完整退货流程（审批、关联原单、退款状态）。调整行只是一条能表达负数的记录，不自动冲减任何数量，也不指向被退的那一笔原始采购。

### 步骤 05（原始发票、审核草稿与逐行确认）：2026-11-10 在 Windows 11（win32）实测

在**干净数据库**（临时目录 + 独立端口）上对运行中的 `python app.py` 逐项核对，**43 项全部通过**：

- **上传与原件**：multipart 上传返回 201、草稿两行、`recognition.is_fake = true`；补传第二页 201、删掉拍坏的那张 200、删最后一个文件 409；`GET .../content` 取回的字节与上传的**完全一致**。
- **不编造**：数值在 JSON 里是字符串；认不出的单价是 `null`、`missing_fields` 为 `unit_price` 与 `food_item_id`；日期与总额因为原文解析不出而留空，但原文仍在 `raw_document_fields` 里。
- **逐行确认**：一次提交两行 → `confirmed=1, failed=1`，**HTTP 仍是 200**，失败那行给出缺失字段；重复确认返回 `conflict` 并指向已有采购记录；`2 × $100` 对 `$226` 先报 `amount_mismatch`，带 `accept_amount_mismatch` 后按 $226 入账并标记。
- **追溯**：采购记录的 `source_invoice` 指回单据 #1 第 1 行，`entry_source=invoice`；未填日期的记录能用 `undated=true` 筛出。
- **匹配与新建**：第二张单据上同一句描述带出历史候选，但行上的食材仍为空（**不自动确认**）；确认时 `new_food_item` 当场建出食材并入账。
- **撤销与收口**：撤销确认 200，采购记录作废且修改日志留痕；收口确认返回「已确认 2 行、跳过 0 行，共 1 个原始文件」；补上单据总额后 `$60 + $226 = $286` 对账为 `balanced`；有已确认行的单据作废被拒 409；`completed=false` 待办清单正确。
- **重启核对**：先确认端口确实无人应答，再重新 `python app.py`——单据仍是 `completed`，两行仍是 `confirmed` 且数值精确（`10 / 6.00 / 60.00` 与 `2 / 100.00 / 226.00`），`amount_mismatch` 仍为 `true`，原始识别证据仍是 `unreadable`，`source_invoice` 仍指向第 1 / 2 行，作废记录只在 `include_voided=true` 时出现，原件 51 字节可读。

迁移：`python -m alembic upgrade head` 到 `e2a825e1dbe5 (head)`，`alembic check` 报 **No new upgrade operations detected**（模型与迁移无漂移），`downgrade base` 可回退。整套测试 `python -m pytest` 在本步骤完成时共 **695 passed, 0 skipped**（步骤 04 时 537）；其中发票相关 **120 项**（`test_invoices_service.py` 83 + `test_invoices_api.py` 37）。总数会随后续步骤继续增长，以这两个文件的项数为准看本步骤的覆盖。

**交付时要说清的**：这一步的识别结果全部来自 Fake 识别器，`is_fake=true` 一路出现在接口响应里。**里程碑 B 的说法是「发票录入与确认闭环可用」，不是「OCR 可用」**；价格分析同样还没有（步骤 09）。

### 步骤 05 追加：删掉一个已经跑过识别的文件（2026-11-12 在 Windows 11 实测）

**修的是一条真实缺陷**：步骤 08 给 `extraction_jobs` 加了 `invoice_file_id` 外键，而步骤 05 的删文件没照顾到它——于是「删掉拍坏的那一张」只要那张照片已经建过提取任务，就抛 `sqlite3.IntegrityError: FOREIGN KEY constraint failed`，返回 **500**。原有用例一直没抓到它，因为它们删的都是 `POST /files` 补传上来的文件，而**补传不触发提取**，那种文件身上根本没有任务。

落点：`services/invoices.py` 的 `delete_file` / `_release_extraction_jobs`，与 `repositories/extraction.py` 的 `for_file()` / `detach_file()`。外键**保持严格**（没有加 `ON DELETE SET NULL`）：能不能删是业务判断，数据库那一层分不清「排队中」和「已跑完」。

在干净数据库（临时目录 + 独立端口 + **真实 `OCR_PROVIDER=dashscope`**）上对运行中的 `python app.py` 实测，两张真实手写页（`05_TangFa_Wholesale_18p__p01.jpg` / `p02.jpg`）一次上传，各建一条提取任务：

- **正在识别时拒删**：上传后立刻 `DELETE /api/invoices/1/files/2` → **409**，`details = {extraction_job_id: 2, status: "queued", file_id: 2, invoice_id: 1}`，消息说明为什么要等（worker 正要按这个文件去读字节，删掉只会换一条看不懂的失败，而那次模型调用的钱照样要花）。文件与字节都还在（`GET .../content` → 200，单据仍是 2 个文件）。
- **跑完之后可以删**：两条任务都 `succeeded`（`crosscheck`、`qwen3.5-ocr` + `qwen3-vl-plus`、`prompt_sha12=12e0246b0377`）后再删 → **200**；`GET .../files/2/content` → 404，另一个文件照常 200。
- **任务历史没丢**：`GET /api/invoices/1/extraction` 仍是 2 条；被删文件那一条保留 `status=succeeded`、`lines_created=1`、模型与提示词标识、原有 14 条 `warnings`，只是 `invoice_file_id` 变成 `null`，并追加一条 `这次提取针对的原件（#2 05_TangFa_Wholesale_18p__p02.jpg）已于 … 被删除；任务记录保留`。另一条任务不受影响。
- **草稿行不跟着删**：那一行仍在，证据里仍记着 `file_id=2`。前端（F06）因此显示「这一行是从一个已经被删掉的原件上读出来的（文件 #2）……不要拿它核对这一行」，识别任务面板显示「原件已不在这张单据上」，裁剪请求回 404 并说「这一行的原件已经不在单据上了」——这条防御提示到这一步才第一次真的跑出来过。
- **外键兜底还在**：回归测试 `test_the_job_foreign_key_stays_strict` 绕开 Service 直接删文件行，仍然撞 `IntegrityError`；把 `_release_extraction_jobs` 那一行注释掉，新增的 3 条用例全部失败（已做变异验证）。

整套测试 `python -m pytest`：**920 passed**（新增 4 条，都在 `tests/test_extraction_api.py` 的「删文件与提取任务的交界」一节）。

### 步骤 06（CSV / Excel 导入导出）：2026-11-10 在 Windows 11（win32）实测

代码由另一条并行工作完成，本节是**独立核对**的结果：在干净数据库（临时目录 + 独立端口）上对运行中的 `python app.py` 逐项核对，**44 项全部通过**。

- **两类文件都能导入**：CSV（中文表头、`2026年11月8日`、UTF-8 BOM）建出 3 行草稿；XLSX 的 `12.345` / `1.0125` **原值进来**、日期列解析为 `2026-11-09`；旧 `.xls` 明确 400 并提示另存。CSV / XLSX 模板都能下载，表头是中文明细列。
- **错误定位到行 / 字段**：`1,234.5` 在默认 `strict` 下报错，定位到**第 3 行、`quantity`、单元格 `B3`**；`03/04/2026` 定位到第 4 行 `purchase_date`；两处的原文都仍在 `raw_fields` 里，解析不了的值**留空**而不是猜。显式选 `number_format=decimal_dot` / `date_order=mdy` 后，同一份文件解析为 `1234.5` / `2026-03-04`。
- **复用同一套流程**：批次的 `recognition.provider` 为空（没走 OCR），行是 `pending`；用步骤 05 的 `/confirm` 逐行确认后，采购记录 `entry_source=import`、`source_invoice` 指回批次第 1 行；原始 CSV 可按字节取回。
- **重传不重复入账**：同一文件重传 → **409 并指回原批次 #1**，采购记录仍是 2 条；改过的文件建新批次 #2，但已入账的那条单价仍是 `6.00`（没被覆盖）。
- **导出可核对**：CSV / XLSX 都能导，含数量、单价、原始描述等明细列；已确认的在里面、**待确认的导入行不在里面**；`kind=adjustment` 只导调整行（`-20.00`）、默认导出只有采购行、`kind=all` 三行用于对账；带 `limit` 直接 400。
- **重启核对**：确认端口无人应答后重启——批次仍是 `ready_to_complete`，三行仍是 `confirmed`（含一行 `adjustment` `-20.00`），`import_metadata` 与原始单元格证据仍在，原始 CSV 280 字节可读，采购记录仍是 `entry_source=import` 并指回批次第 1 行。

迁移：`6f0c81da2306`（导入批次与行级待复核信息），`upgrade head` / `downgrade base` 都通过，`alembic check` 报 **No new upgrade operations detected**。表格相关测试 `test_spreadsheets.py` **64 项**，整套 `python -m pytest` 全绿。

**一处要说清的**：这一步的代码先于文档完成（当时 `stru_dev.md` 步骤 06 仍写着「具体流程待定」、README 还写着「未实现」）。第 3 节的决定与本节记录是**按实现补写**的，代码没有改动。

### 步骤 07（真实单据与 OCR 候选评估）：2026-11-10 在 Windows 11（win32）实测

**这一步不产出运行时代码**，产出是评测工具、Ground Truth 与一份选型记录。逐项验证见 [docs/impl_record.md 步骤 07](docs/impl_record.md) 第 6 节。

- **实跑规模**：3 个可用型号 × 78 张图 = 234 次成功调用，另 78 次因产品未开通失败。单页耗时中位 5—7 秒，整轮约 148 万 tokens。
- **提示词一致性可核对**：234 份结果各自记着所用提示词，sha256 前 12 位**全为 `12e0246b0377`**——四个候选用的确实是同一份提示词，换提示词就是换一个被测配置，不能混比。
- **密钥不泄漏**：对 `samples/`、`tools/`、`docs/`、`var/` 全目录搜索 `.env` 里的 Key，**0 命中**；`samples/real/` 与 `.env` 都在 `.gitignore` 内。
- **GT 自检**：6 张印刷件 GT **全部算术自洽**（每行 数量×单价 对得上行金额，各行之和对得上印着的 Total，容差 $0.05 同步骤 04）。4 份小票 GT 的算术提示全部有据（折扣价与原价分列、税额差），已在各自 `notes` 说明。
- **行结构检测**：`python tools/ink_rows.py samples/real/pages` → 74 页中 **68 页判定成功、6 页诚实标为未判定**。去斜是必需的：不去斜时有 37 页一条横线都找不到。
- **既有测试不受影响**：`python -m pytest` **761 项全绿**（本步骤未改动 `src/`）。

**一处要说清的**：Check List 07.8a 的虚报率判据**与原计划不同**。原定「墨迹判空 + 模型报值 = 确证编造」，但逐行墨迹判空没能实现——空行与写了字的行在三种基准下像素密度全部重叠。改用「行容量（全自动）+ 人工核对子集（3 页）」两级判据，失败数据与理由都留在附录，没有用一个调不准的阈值冒充结论。

### 步骤 10（搜索增强、最近/常用、食材合并 —— **后端**）：2026-11-14 在 Windows 11（win32）实测

逐项验证见 [docs/impl_record.md 步骤 10](docs/impl_record.md) 第 10 节（按 Check List
条目编号排列）。三条实际跑过的：

- `python -m pytest` —— **1207 项全绿**（步骤 09 结束时 1077 项，本步骤新增 130 项）。
- `python -m alembic upgrade head` —— 新增三条修订（`food_item_merges` 表、
  `food_items.needs_review` 列、`Uncategorized` 分类 seed）在新临时库上执行通过；
  `test_models_match_migrations` 确认模型与迁移没有漂移。
- **真实样本实跑**：`tools/e2e_keywords.py --all-handwritten --workers 4` ——
  **78 份真实样本、真调模型、4.6 分钟、78/78 成功、17/17 条判定通过**，报告
  `samples/real/eval/e2e_step10.json`。另有两轮**真实 HTTP 实跑**（起
  `python app.py` 打真实请求，不走测试客户端）：第一轮覆盖建议搜索（三种真实写法
  的模糊命中）、最近/常用、描述映射两层与「从没买过」筛选，**16/16 通过**；
  第二轮覆盖「未分类」锚点（不许删、可改名）、关键词自动认食材与 `needs_review`
  的置位与清除、收口面板的分类汇总与调整行单列一桶、合并的预览 → 执行 → 旧叫法
  仍可搜、以及被拦住时什么都没动，**22/22 通过**。

⚠️ **两处边界与两项未勾选**，说结论时要一起说：小票走不到关键词路径（被手写闸门
挡在更前面）、「单位归一不出来就不建」只有一行真实样本验得到；10.14 / 10.17 是
前端项，按当日与用户确认的范围不在本轮。

### 步骤 09（单位换算、月度价格、来源比较与涨价提醒）：2026-11-13 在 Windows 11（win32）实测

逐项验证见 [docs/impl_record.md 步骤 09](docs/impl_record.md) 第 6 节（按 Check List 条目编号排列）。

- **测试**：`python -m pytest` → **1207 项全绿**（步骤 09 结束时 1077 项，步骤 09 之前 924 项）。步骤 10 新增 130 项，其中 `test_matching.py` 22 项、`test_suggestions.py` 17 项、`test_merging.py` 21 项、`test_keyword_items.py` 37 项。
- **迁移**：`python -m alembic upgrade head` / `downgrade -1` 在新建的临时库上各跑一遍，两张新表建出又删掉、`PRAGMA foreign_key_check` 无输出；`uq_pack_specs_item_source_unit` 与 `uq_pack_specs_item_any_source_unit` 两条**带 WHERE 的局部唯一索引**在库里确认存在（`sqlite_master` 里能看到 `WHERE source_name_normalized IS NOT NULL` / `IS NULL`）。
- **09.10 的三个验收算例**各有一个用例，名字都带 `acceptance`：10 lb / \$60 → `6.0000`/lb；1 case = 40 lb、\$220 → `5.5000`/lb（走箱规那条路）；10 lb × \$5 与 100 lb × \$6 的加权均价 → `5.9091`/lb（≈ \$5.91），同一个用例断言它**不等于**简单平均的 \$5.50。
- **真实数据实跑**：`python tools/e2e_prices.py` → **43 行真实采购全部算出标准化价格**、**0 次模型调用**、2.5 秒，报告 `samples/real/eval/e2e_step09.json`。其中 5 页数字 PDF 走**真实提取链路**（上传 → 文字层 → 草稿 → 29 行逐行确认 → 采购记录 → 价格）；6 张印刷件用步骤 07 的**人工 GT 值**（报告里 `source: human_ground_truth`，不是本次模型读的）。
- **真实箱规**：待补清单 3 组 → 确认 3 条后清空。其中「1 case = 10000 g」是**从真实商品描述 `1000g x 10` 读出的候选**经确认生效（Kun Kee 的真实写法），最终价格 **\$0.0052/g = \$5.20/kg**。
- **实跑抓到并修掉的三个真问题**（细节见 impl_record.md 步骤 09 4.1）：① 尾部单位表只有 lb / kg / g / each 几个写法，真实单据上 `72 heads`、`16 gal.`、`48 cans` 等 **7 行的数量被静默判空**，价格一条也算不出 —— 现在那张表覆盖全部单位写法并由测试断言一致；② 统计窗口 `date_to=2026-12-31` 被**录入日期**的「不许未来」规则拒掉（查到年底是正常请求）；③ 按箱买的货基础单位落成 `each` 时箱规存不进去（实跑脚本的取舍问题，暴露了真实使用顺序）。

⚠️ **三处真实样本验不到的东西**（不要说成已验证）：真实单据里没有一条食材是从两个来源买的，**来源比价只有 pytest 覆盖**；真实采购全部发生在 2026 年 6 月而实跑那天是 11 月，**月度环比也只有 pytest 覆盖**（比较结果诚实地是 `no_current_data`）；4 张零售小票共 **34 行录不进来**——纸面没有计量单位。**真调模型的对照轮已执行**：`python tools/e2e_prices.py --with-ocr` → **12 次真实调用、36 秒**，6 张印刷件 14 行逐行比对，**每单位价格与 GT 相同 12 行、不同 0 行**（11 行入账并算出 $2.69—$6.39/lb），2 行被交叉裁决判为需人填、1 行因缺单位被正确拒绝入账。逐行数据在报告的 `with_ocr.pages[].comparison` 里。

### 多文件计划 M03（图片/PDF 的批次摄入）：2026-11-21 在 Windows 11（win32）实测

- **测试**：`python -m pytest` → **1324 项全绿**（M02 结束时 1276 项；本步新增 `test_intake_ingest.py` 26 项、`test_intake_api.py` 16 项）。**没有迁移**：切页的待办状态用的是 M02 已有的 `intake_batch_files.status`。
- **真实原件实跑（纯本地计算，0 次模型调用、不花钱）**：`samples/real/` 的 10 份原件（5 份多页 PDF、1 份数字 PDF、4 张图片）走完整摄入通路 → **83 个可归类内容**，与 M00 清单独立数出来的 83 个内容单元一致。页数与文件名标注逐项吻合（32p→32、18p→18、1p→1、**5p→5**、18p→18），4 张图片各 1 页；盘上恰好 10 份字节；**83 页逐页回看，SHA-256 与原文件完全一致**；83 页全部留在「待归类」一个组里，`decided_*` 全空、候选 0 条。数字 PDF 5 页报出有文字层、74 页扫描页报出有整页图——与步骤 07/M01 记录的「本地数字 PDF 文字层 5 页」对得上。报告在被忽略的 `samples/real/m03/m03_real_ingest.json`。
- **真实本机服务器实跑**（`python app.py` + 临时库，`OCR_PROVIDER=fake`，用完已删）：真实 5 页 PDF 上传得到 `queued=true` / `pending` / `ingest=null`，轮询后 `ready` + `item_count=5`，5 个页引用逐个 `GET .../items/<id>/original` 返回 200、`X-Intake-Page` 为 1—5、字节与原文件 SHA 一致。**12 MB / 32 页真实 PDF 的上传请求 0.07 秒返回、0.30 秒后 ready**——切页确实在 HTTP 请求之外。另验：跨批次 id 读原件 404 / 同批次 200；同批次重复文件 409；CSV 400 并给出受理扩展名；真实 PDF 的截断副本 → 201 + `failed` + 「PDF 打不开：Stream has ended unexpectedly」且同批次其它文件不受影响。
- **中断后重试**：用另一个进程在同一个库/存储上留下一条没人管的 `pending`（与进程被 kill 后剩下的状态一样），服务器 `POST .../process` 把它切成 18 页并逐页可回看。
- **实跑抓到并修掉的两个真问题**：① 上传响应里的 `file` 用的是**登记那一刻**的快照，当场切完时仍报 `pending`——改成以现状汇报；② 页已切好、之后被别处标成 `failed` 的文件重试会撞 `(batch_file_id, page_no)` 唯一索引变成 500——改成按 `item_count` 判「已经切过」，返回 `already_ingested` 且页的 id 不变。两者都有专门用例。

⚠️ **不要说成已验证的几件事**：`samples/real/` 里**一张 HEIC 都没有**，缺解码器时「照样登记一页 + 留一条 note」只有合成路径验过；摄入阶段不产生任何日期/商家候选，所以 M05 的分流、M06 的归组在这一步无从验证；可注入处理器只用测试替身验过契约；**没有任何前端与浏览器操作**；「某页失败」在本步收敛成「整份文件失败」——pypdf 的页枚举是全有或全无，逐页成败要等 M05 的逐页识别，且 notes 只在返回值与日志里，**没有落库**。

### 多文件计划 M05（分类候选与日期解析）：2026-11-21 开发 / 2026-11-22 真实 OCR 抽样，均在 Windows 11（win32）实测

- **测试**：聚焦跑 M05 与相邻步骤的 8 个测试文件 → **196 项全绿**；本步完成时 `python -m pytest` → **1372 项全绿、退出码 0**（M03 结束时 1324 项；本步新增 `test_intake_recognition.py` 29 项、`test_intake_recognition_api.py` 10 项、`test_normalization.py` 9 项）。⚠️ 之后并行开发的 M06 把自己的两张表与四条路由加进同一工作树，最后一次全量变成 **1374 passed / 3 failed**，那 3 项全部是 M06 的清单型断言（业务表清单、两处路由清单），**断言里没有一项提到 M05 的产物**。
- **迁移 `c47be2a91f85`**：只给 `intake_items` 加五列（`recognition_status`、`recognized_at`、`recognition_payload`、`recognition_error_code`、`recognition_error_message`）与一条索引，不加表、不改既有列。`upgrade head` → `compare_metadata` 为 `[]` → `downgrade -1` → 再 `upgrade head`；**逐条核对两条局部唯一索引（`uq_intake_items_page` / `uq_intake_items_row`）在批处理重建后仍带 `WHERE`**——这正是这一列刻意不加 CHECK 的原因。
- **真实原件实跑（纯本地、0 次模型调用、不花钱）**：`python tools/eval_multifiles_m05.py local` 把 `samples/real/` 的 10 份原件走完整通路 → **83 个可归类内容**（与 M00/M03 独立数出的 83 一致），**全程 0 次模型调用**。真实数字 PDF 的 5 页走文字层拿到日期候选，**5 页的纸面日期全部是歧义写法**（`6/11/2026` ×3、`6/10/2026`、`6/9/2026`），各留两条读法；另外 78 页被「拒绝被调用」的守卫挡下（即「需要 OCR」的清单）。**再驱动一次队列：0 条待办、0 次调用。** 随后对那 5 页确认商家并把这家 `date_order` 设成 `mdy` → 每页**恰好一条**日期候选（`2026-06-11` ×3、`2026-06-10`、`2026-06-09`）、**0 次调用**、`decided_purchase_date` 仍为 `null`。
- **真实本机服务器实跑**（`python app.py` + 临时库，`OCR_PROVIDER=fake`）：上传真实 5 页数字 PDF → 5 页 `pending`（摄入钩子排队，排队不调模型）→ `POST .../recognize` 得到 5 条 `recognized` / `text-layer` / **`model_calls=0`**、两条读法俱在 → **再 POST 一次：`results` 为空、`model_calls=0`** → `PUT /api/ocr-routes` 设 `date_order=mdy`、确认商家后逐页 `POST .../reparse-date` → 每页一条日期、`model_calls=0`、`decided_purchase_date` 仍为 `null`。
- **用户授权后的真实 OCR 抽样（2026-11-22，10 页、20 次调用，提示词 SHA `67fb7df1217b`，两模型交叉裁决）**：用户在「全量 78 页 / 10 页抽样 / 不外发」中选了 10 页抽样。**5/10 读出商家候选**（4 家印刷体抬头；同一份 PDF 的第 1 页与第 7 页读出同一家）；**手写件与 4 张照片一条商家候选都没有**（`unreadable` 或两模型分歧 → 不编造名字）。**4/10 读出日期原文，其中 3 条是歧义写法**（`6/1/26`、`6/3/2026`、`09-02-22`）各留两条读法，1 条（`06/26/2026`）文本自身定死 → 一条候选。识别请求里 `original_name=None`，所以商家名来自纸面而不是文件名。**对这 10 页再驱动一次：全部 `already_recognized`、0 次调用**——「不对同一页重复收费」在**付费的真实数据**上验过。原始响应只在被忽略的 `samples/real/m05/`。

⚠️ **不要说成已验证的几件事**：真实 OCR **只覆盖 10 页**，另外 68 页扫描件未外发，商家候选的真实覆盖率与准确率**不能**由这 10 页外推到 83 个单元，也没有逐项人工真值可比对；真实数字 PDF 的 5 页**读不出商家**——卖方名是页首一行**没有标签**的标题，而页上唯一带标签的一方是 `Bill to:` 的**买方**，所以这里宁可留空让人命名一次（跨批次复用那个名字是 M07）；第 3.2 节的第 2—4 层（同文件参考、历史别名、相似版式堆）**一层都没做**；`recognition_payload` 里的行项目是给 M08 复用的，审核那一端的「不重复收费」**尚未端到端验证**；**没有任何前端与浏览器操作**；仍然没有真实 HEIC 原件。

### 版本组合

验证通过的版本组合：

- Python：3.13.1（本机另有 3.12 可用）
- pip：26.3
- 直接依赖：Flask 3.1.3、SQLAlchemy 2.1.8、alembic 1.20.1、python-dotenv 1.2.4、openpyxl 3.1.5、defusedxml 0.7.1、pytest 9.2.0
- 表格依赖的选择（步骤 06）：`openpyxl` 提供列 / 样式工具与 XLSX 写出；`defusedxml` 用来解析工作簿 XML（挡 XXE 与实体炸弹）。**读入不走 openpyxl 的常规 reader**——它会把十进制单元格先转成 `float`，等于把步骤 04 刚解决的精度问题又带回来，所以直接读 OOXML 的 `<v>` 文本。

`python -m pip freeze` 的完整解析结果：

```text
alembic==1.20.1
blinker==1.9.0
click==8.5.1
colorama==0.4.6
defusedxml==0.7.1
et_xmlfile==2.0.0
Flask==3.1.3
iniconfig==2.3.1
itsdangerous==2.2.0
Jinja2==3.1.6
Mako==1.4.3
MarkupSafe==3.0.4
openpyxl==3.1.5
packaging==26.3
pluggy==1.7.0
Pygments==2.21.0
pytest==9.2.0
python-dotenv==1.2.4
SQLAlchemy==2.1.8
typing_extensions==4.16.0
Werkzeug==3.1.9
```

`requirements.txt` 只固定直接依赖，上表是本次验证时的完整快照。升级依赖时同步更新本节与 `requirements*.txt`，并重新执行一次干净环境安装；`test_baseline.py` 会在声明与实际安装不一致时直接失败。

## 15. 数据与密钥边界

- 本机配置 `.env`、虚拟环境 `.venv/`、SQLite 数据库与上传文件（`var/`）、真实单据与人工标注（`samples/real/`）都在忽略规则内，不进入源码交付。
- 合成回归样本随项目交付，放在 `samples/synthetic/` 并保持可见（步骤 07.10 已产出 `text_layouts/`）。
- **步骤 07 的评测产物全部落在 `samples/real/` 之下**（识别结果 `ocr/`、Ground Truth `ground_truth/`、行结构 `ink/`、评测报告 `eval/`），随该目录一起被忽略——它们含客户单据内容，不进入源码交付。要复现，按 `docs/impl_record.md` 步骤 07 用 `tools/` 下的脚本重跑。
- **识别结果文件不含凭据**：`ocr_batch.py` 只从环境变量 `DASHSCOPE_API_KEY` 读 Key，**不接受命令行传入**（命令行参数会进 `ps` 与 shell 历史），输出文件只记模型名、提示词、用量与响应内容。
- 密钥只从环境变量读取，不写入源码、测试样本、日志与文档。
- 当前是本地单工作区演示：没有登录、权限与多用户隔离，不适合公开访问。共享部署时的托管、数据库/文件持久化与访问控制在步骤 11 单独落实。

## 16. 转写来源记录

从旧项目**转写的代码与样本**在此登记。步骤 00—06 没有转写任何代码；步骤 07.10 转写了**样本**（不是代码）；步骤 09 转写了**换算思路与四条测试用例**。

| 本项目文件 | 参考来源 | 修改点 | 对应测试 |
| --- | --- | --- | --- |
| `src/services/units.py` | `kitchen-ledger/web/recipes.js` 的 `canonical` / `units` / `convert`（约 4 行压缩代码） | ① 浮点改 `Decimal` 并**拒绝 `float` 入参**；② 去掉 `from === to` 的短路——两边都必须是认识的计量单位（见下）；③ 单位从 7 个补到 12 个并写明**美制体积**；④ 包装单位**不进换算表**，改走箱规；⑤ 没有复制菜谱、库存与 `domain.js` 依赖 | `tests/test_units.py`（55 项） |
| `tests/test_units.py` 的 `test_transcribed_*` | `kitchen-ledger/tests/recipes.test.mjs` 的 `unit conversions enforce dimensions` | 四条断言里**三条照搬**（`150 g → 0.15 kg`、`1 lb → 453.59237 g`、`1 l → kg` 抛错），**第四条刻意改成相反方向**：旧测试断言 `convert(2,'case','case') === 2`，而那是 `from === to` 短路的副产品——它会把任何拼错的单位也放过去（`convert(2,'lbb','lbb')` 同样返回 2）。本项目要求「同单位也校验输入」，所以 `case → case` 必须报错并提示走箱规。改动理由写在用例的文档字符串里 | `test_same_packaging_unit_is_refused_unlike_the_legacy_test` |
| `samples/synthetic/text_layouts/*.txt` 与同名 `.json` | `kitchen-ledger/tests/fixtures/invoice-layouts.json`（6 个英文版式） | 字段名换成 `src/contracts.py` 的标准名；数值从 JS 浮点转成字符串按 GT 口径存；**丢弃 `baseQuantity`**（换算后的派生值，换算属步骤 09，GT 不存派生值） | 暂无自动化测试——它们是步骤 08「数字 PDF 直接抽文字层」那条路径的**回归输入**，解析器要到步骤 08 才写 |
| `samples/synthetic/text_layouts/freshfields_ocr_output.txt` | `kitchen-ledger/tests/fixtures/freshfields-ocr.txt` | 原样复制为本项目副本 | 同上 |

生成脚本 `tools/gt_from_legacy_fixtures.py` **只在开发时读一次旧目录**；运行时与测试时都不碰。⚠️ 这批样本是**英文、合成、而且是文本不是图像**，**不作为中文手写 OCR 的性能证据**，也**不把旧的 Tesseract 英文方案设为默认**（步骤 07.10 的要求）。

步骤 03 **审阅后未采用**的旧实现（对应 `docs/stru_dev.md` 的 Check List 03.4）：

| 旧实现 | 不采用的原因 | 本项目的做法 |
| --- | --- | --- |
| `kitchen-ledger/supabase/migrations/001_restaurant_ledger.sql` 的 `ledger_products` 表 | 只有单个 `name`（没有中文名）、`category` 是自由文本（无法维护分类）、还带 `storage_area` / `track_stock` / `reorder_level` 等库存字段——库存不在本轮范围 | 重新设计 `categories` + `food_items`：分类是独立表且可维护，英文名与中文名分列且各自唯一，没有任何库存字段 |
| `kitchen-ledger/web/domain.js` 第 7 行 `add('supplierId','Choose a supplier.')` | 强制「必须先选供应商」。老板直接去超市采购时没有供应商可选，这条规则会让数据根本录不进来 | 食材不带供应商字段；采购来源是步骤 04 采购记录上的**可选**字段 |
| `kitchen-ledger/web/document-parser.js` 第 69 行 `matchProduct()` | 用商品名拼正则去匹配发票描述，只在「恰好命中一条」时才算匹配，且完全不处理中文与别名 | 步骤 03 用规范化后的名称列做确定匹配与包含搜索；模糊匹配与历史纠正记忆归步骤 10，依据是步骤 04 积累的采购原始描述 |

这三条都只是**阅读与否决记录**，没有复制任何代码，也不构成运行依赖。

步骤 09 **审阅后未采用**的旧实现（对应 `docs/stru_dev.md` 的 Check List 09.9）：

| 旧实现 | 不采用的原因 | 本项目的做法 |
| --- | --- | --- |
| `kitchen-ledger/web/app.js` 的 `reports()` | 只按**原始计价单位**汇总，不换算也不区分包装规格，于是「一箱」和「一磅」会被加在一起；而且统计口径与库存、菜谱消耗耦合在一起 | `services/prices.py` 重新实现：先换算到基础单位再加权，调整行与已作废记录永不参与，算不出来的行逐条给出原因 |
| 旧项目的库存图表与菜谱消耗统计 | 库存与菜谱不在本轮范围（`imp_notes.md` 明确排除） | 不实现；价格统计只读 `purchases` |
| 旧项目按 USD 写死的金额口径 | 老板也会在国内采购（真实样本里就有 RMB 小票） | 币种是采购记录上的列（USD / CNY），统计**按币种分开**，多币种时报错而不是相加 |

这三条同样只是**阅读与否决记录**。

步骤 02 只借鉴了两条**原则**，没有复制任何代码，也不构成运行依赖：

| 原则 | 来源 | 本项目的实现方式 | 对应测试 |
| --- | --- | --- | --- |
| 事务的成功与失败要分清，失败不留下半条记录；结果未知时不声称已回滚 | `restaurant-erp` 的写函数以显式异常中止整个事务 | `Database.unit_of_work()`：异常回滚并原样抛出；回滚本身失败时记录「结果未知」。未移植 SQL 角色、存储过程、公开视图、Pooler、RLS，也没有把 PostgreSQL 的行锁搬到 SQLite | `test_db.py::test_failure_rolls_back_the_whole_operation`、`test_nested_failure_discards_inner_writes_too` |
| 原始文件要可追溯，且文件引用不能越出自己的存储范围 | `kitchen-ledger` 把原始单据存在对象存储并在记录里保存引用，写入前校验路径归属 | `LocalFileStorage`：键由适配器生成，元数据保留原始文件名与摘要，每次定位都校验路径边界。未沿用其存储桶配置、策略与浏览器端实现 | `test_file_storage.py::test_save_records_metadata_for_traceability`、`test_keys_outside_the_storage_root_are_rejected` |
