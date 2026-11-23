# 多文件录入开发计划：按采购日期与商家整理

> 2026-11-21 方案稿，供开发前审核。范围覆盖前端与后端；本文是计划，不表示功能已经实现。现有单据审核、采购确认和价格历史规则仍以 `imp_notes.md`、`stru_dev.md`、`struct.md`、`backend/README.md` 及代码为准。实施中的决定和验收结果按现有约定追加到 `impl_record.md`。

## 1. 本次要解决的问题与已确定的产品决定

餐厅用户一次上传十几份混合文件，可能包括 PDF、JPG/JPEG、PNG、HEIC、CSV、XLSX。现有上传入口把所选图片/PDF 当作**一张发票的多个文件**；CSV/XLSX 则走独立表格导入。用户需要的是将这批内容整理成可审核的采购数据，而不是把所有文件塞进一张发票。

**分类只有两个主要业务维度：实际采购日期、商家来源。**「同一天、同一商家」构成整理页的最小组；一个组可以包含多张不同发票、不同文件的页或表格行。一份 PDF 的不同页可以进入不同日期/商家组。文件归属、页序和文件名只作为推断线索与回看原件的信息，不成为界面上的一级分类。

流程为：**混合上传 → 按日期和商家整理 → 在组内填写与确认 → 进入采购价格历史**。整理不等于入账；现有的人工逐行确认、整单收口、原始证据和修改日志继续生效。允许先处理已经整理好的组，待定内容留在批次里以后再办。

桌面端采用左侧待归类卡片、右侧「日期 → 商家」目标组。支持单张和多选拖拽；拖到日期标题只定日期，拖到某日期下的商家组则同时确定两项。手机端以「点卡片/选多张 → 点目标组」完成同一操作，拖动可作为快捷方式，不能是唯一入口。财务人员另有表格视图，两种界面使用同一份批次与审核数据。

下图是已讨论的低保真结构，不是最终视觉稿：

![左侧待归类、右侧按日期和商家分组的整理页低保真图](multifiles-wireframe.png)

纯文本结构：

```text
待归类（可勾选多张、拖动）       11 月 8 日
  相似的 3 张，商家未知   ───→    Costco / Restaurant Depot / 商家待定
  日期不清，商家未知             11 月 9 日
                                   Costco / 华丰商行 / 商家待定
拖到日期标题：只定日期；拖到商家组：日期与商家一起确定。
```

## 2. 调研结论与本项目的实际限制

- [Dext 的说明](https://help.dext.com/en/articles/416691-what-information-does-dext-extract-from-costs-and-sales-documents)指出，它会参考账户中相似的历史单据选择商家，人工纠正会影响后续匹配；[商家别名](https://help.dext.com/en/articles/106015-how-to-manage-suppliers-in-dext)用于把不同写法归到同一来源。这启发本项目保存用户确认过的来源映射，但不能把视觉相似当成事实。
- [Atlassian 拖拽指南](https://atlassian.design/components/pragmatic-drag-and-drop/design-guidelines)建议多选拖动使用带数量的叠放预览、明确显示可拖动区域；[W3C 的拖拽替代操作](https://www.w3.org/WAI/WCAG22/Techniques/general/G219)要求能通过点选等方式完成同一操作。因此整理页提供拖拽、多选批量移动和点选目标组，三者执行同一后端命令。
- 版式相似可帮助形成待确认的「相似一堆」；它不能证明商家相同。[模板文档研究](https://vldb.org/pvldb/vol14/p997-tata.pdf)说明同一模板常共享布局，而[发票识别研究](https://cs229.stanford.edu/proj2016/report/LiuWanZhang-UnstructuredDocumentRecognitionOnBusinessInvoice-report.pdf)指出相似版式的细小差异会造成误配。本项目只把相似性用于建议和批量人工命名，自动归组的条件须由本项目样本验证。
- 当前 `src/integrations/dashscope.py` 的 `CONTENT_PROMPT` **没有询问 `supplier`**。解析器将缺键标为 `NOT_ASKED`，不能把当前来源为空解释为「模型识别失败率 70%」。现有基准结果绑定旧提示词 SHA `12e0246b0377`；改提示词后旧准确率只能保留为历史基线，须重新评测。
- `ocr_routes` 按已知来源选提取路径和 `date_order`。若来源未知，美式数字日期也可能因日/月歧义保留为空；新流程必须能在**没有来源路由**时先处理来源与日期候选，不能形成「先知道商家才能识别日期、先有日期才能归商家」的循环。
- `invoice_files` 目前一份文件只归一张 `invoice`，并以 `sha256` 全局判重；`POST /api/invoices` 的多文件含义是同一张单据的多页。CSV/XLSX 目前以一个 `entry_source='import'` 的审核容器保存整份表。这些结构不能直接表达「一份 PDF 的不同页或一份 CSV 的不同行进入不同日期/商家组」。

## 3. 来源识别：新增模型输出与人工纠正

### 3.1 模型应回答什么

为单据抬头/零售小票补充商家识别。要求识别**卖方/开票方/商家**，明确排除买方、收货方、付款方及商品品牌。只有纸面确实出现时才给候选；看不清时标为不可读，纸面没有时标为缺失，不依据食材、历史价格或上传日期补造来源。

中英文同时印出时，**用于分组和展示的名称优先取纸面上的英文名；没有英文名才取纸面上的中文名**。不把中文自行翻译成英文，也不凭英文缩写扩写法定名称。建议让模型分别返回纸面英文名、中文名及可得的原文证据，再由后端统一选择展示名；原始两个读法都留在识别证据中。两模型不一致、无法判断谁是卖方、或模型只给翻译而纸面无对应文字时，保留候选并要求人工确认。

实施时需同步修改提示词、适配器解析、统一识别契约映射、跨模型裁决、草稿证据、API 响应、前端候选呈现、提示词版本记录和相关回归测试。旧基准样本与旧 SHA 不覆盖；新提示词以新 SHA 记录。评测按现有印刷件、手写件、零售小票、数字 PDF 分层；缺逐项真值的类别只报告可核对的行为和覆盖范围，不编造准确率。数字 PDF 优先利用文字层；不能假定新提示词在所有类别上同样有效。

### 3.2 来源读不出时如何减少人工工作

来源建议按可解释线索分层提供，而不是要求用户逐张手填：

1. **纸面直接读出**：保留文字/页码证据，提出商家候选；不是自动入账。
2. **同文件参考**：同一文件中已有用户确认的商家时，为其他页提出候选；若某页明确写着不同来源，单页证据优先。
3. **历史来源与别名**：规范化大小写、空白和用户确认过的中英文/简称写法，避免 `Costco`、`COSTCO` 分成两组。相似名称只建议，不自动合并不同商家。
4. **相似外观的待办堆**：没有可读商家的扫描件按可解释的版式特征形成候选堆，展示缩略图供用户扫视；用户可移出例外，再给整堆选择一次来源。首版先验证固定模板/布局特征，是否需要图像嵌入模型由实测决定。
5. **仍然未知**：留在某日期下的「商家待定」或全局待归类区；可继续处理其他内容。人工确认一次后，记住来源和适用线索，供后续批次建议使用。

“十张 JPG 中七张来源未知”是用户此前描述的使用经验，**不是已测量的 OCR 失败率**；那组 JPG 不在当前仓库，后续开发不以取得它们为前置。能取得人工真值时，评测分别统计纸面没有来源、纸面有但模型未读出、模型读错、提示词未询问；无法判定的类别明确写“未测”，不阻断功能开发。

## 4. 整理页的交互细则

- 待归类卡片只突出缺少的关键项、原件缩略图和建议依据，例如「纸面读出」「参考同文件」「与历史单据外观相似」。点击卡片打开原件预览；卡片上的操作保持精简，移动与更多操作收在一个菜单中。
- 拖动一张到日期标题：只写日期，来源仍为未知；拖动到该日期下的商家卡片：同时写日期和来源。日期/商家组是大面积投放目标，拖动时高亮可投区域，非法目标不接收。多选时显示叠放预览和张数。
- 拖错后可立即撤销；批量移动只改变被选中的项目，后端一次提交并返回逐项结果。刷新和换设备后保持结果；两个会话同时修改时使用版本号提示冲突，不默默覆盖。
- 手机端先点选一张或多张，再点目标日期或商家组；日期未列出时保留日期选择器。要验证长按拖动与页面滚动、PDF 预览手势是否冲突；手机功能不依赖拖拽。
- 财务表格视图按同一分类状态筛选、多选与批量改值；上方/侧边可看原件。它与卡片视图共用后端分类命令，切换视图不复制草稿。
- 系统可先把高可信候选**显示**在相应组里，但必须保留「系统建议／人工确认」区别。确认采购行前应能看见并改正组的日期与来源。来源不明不会变成一个编造的商家名。

## 5. 后端边界与数据流

建议增加持久化的上传整理批次，而不在上传第一张文件时立即创建正式的单据草稿：

```text
上传批次 → 原文件引用 → 可归类内容（PDF 页、图片、表格行）
                         ↓ 日期/商家候选、依据、人工决定、版本
                  按（采购日期，规范化来源）展示为组
                         ↓ 进入填写
                 复用现有发票草稿/导入行、逐行确认与采购记录
```

原件字节只存一次；一个可归类内容要能回指其文件与页码或表格行号。日期/商家组是**整理视图与工作队列**，不等于一张发票：同组可能有几张真正的发票，单据总额、重复提示与收口仍分别处理；不能把同组金额合成伪发票。混合页（同一页确实含不同日期/来源）进入例外审核，可在行级设置覆盖值，不强行给整页一个错误组。

API 至少需要：创建/恢复批次、逐文件上传与处理状态、读取分页的待办和分组、批量改日期/商家并带版本、撤销最近一次分类操作、把已整理内容送入现有审核、从组回看原件。具体 URL 和表结构在实施步骤中细化。OCR 与解析后台执行；浏览器关闭不丢任务。整理阶段已得到的识别结果应复用，避免送入单据草稿后对同一页重复调用模型。

现有 `source_name_normalized` 可继续作为已确认采购记录的比较口径，但来源别名/模板记忆需要有独立、可更正的映射。商家名未确认前，不应仅凭相似度为它应用某商家的 `ocr_routes`；用户确认来源后，可用该商家的 `date_order` **重新解析已保存的日期原文**，不必为此重复 OCR。已入账后若改组，必须显式更新相关采购记录并保留修改日志，不能只移动界面卡片。

## 6. AI coding 执行约定与总清单

每个 `Mxx` 是**一次相对独立的 coding 任务**。把本文件交给 AI 时明确指定当前步骤；完成该步及其必要的共用代码即可。后端继续采用 API → Service → Repository 分层，原件处理放 `documents/`，外部模型调用放 `integrations/`。不要把示意 API 或表名当成未经核对的最终契约：在对应步骤内先查现有实现，再定迁移与接口，记录取舍。

**本地文件读取约定（Windows、仓库路径含中文）**：从仓库根目录执行命令，引用实际文件路径；PowerShell 读取中文 Markdown 时使用 `Get-Content -LiteralPath <路径> -Encoding UTF8`，写入时也明确使用 UTF-8。路径要先核对，例如开发说明位于 `backend/docs/stru_dev.md`，不要把根目录下找不到同名文件误判为文档缺失。命令报 `helper_unknown_error: setup refresh had errors` 时，表示执行器尚未成功启动，不能据此判断文件内容、路径或权限有问题；先用最小只读命令确认执行环境，必要时换用获准的执行方式，并如实记录仍未知的底层原因。中文路径需要正确引用和编码处理，但没有证据表明它导致了该执行器错误。

**可用材料边界（2026-11-21 用户确认）**：本计划只以当前 `backend/samples/real/` 的材料为真实样本来源，不再要求用户补交十张 JPG、跨来源 CSV 或其他原件。已有样本能验证的内容用真实样本；缺失的版式和跨组边界用明确标为合成的夹具做代码、接口与浏览器功能测试。样本不足、OCR 凭据缺失或设备不可用时，写清未测范围，但不得把取得额外材料设为开发或步骤完成的前置。优先交付可操作、可恢复、可回看原件的完整功能；不能用合成结果宣称真实 OCR 准确率。

每步交付须写出：修改文件、迁移或接口变更、使用的样本与验证命令、真实运行结果、未覆盖的风险。将记录追加在该步的「验证记录」后；新实现的详细取舍和被否决方案仍追加到 `impl_record.md`。**子项开发完成并通过相应验证后，立即把该子项的 `- [ ]` 改成 `- [x]`；该步全部子项、测试和本计划按现有材料可完成的验收要求满足后，立即把总清单中的该步骤改成 `- [x]`，不等待用户另行审核。未完成或未验证的项保持 `- [ ]`，并在验证记录中说明原因。** 真实 OCR、历史结果回放、合成测试和浏览器操作分别记录；缺少真实材料不阻断已实现功能的验收，也不能把合成测试写成真实识别结论。

下表中的「可独立」指可与同阶段其他任务分别开发、分别验收；共享文件发生冲突时仍须顺序合并。现有单张录入是回归基线，不因新批次功能而消失。

- [x] **M00 现有样本清单与基线**（前置：无；研究与评测）
- [x] **M01 模型提取商家**（前置：M00；后端 OCR；可与 M02 独立）
- [x] **M02 整理批次的数据与存储边界**（前置：无；后端基础；可与 M00、M01 独立）
- [x] **M03 图片/PDF 的批次摄入**（前置：M02；后端文件处理）
- [x] **M04 CSV/XLSX 的批次摄入**（前置：M02；后端表格处理；可与 M03 独立）
- [x] **M05 分类候选与日期解析**（前置：M01、M03、M04；后端识别流程）
- [x] **M06 人工归组、批量移动与撤销**（前置：M02、M03；后端分类命令；可用人工候选独立于 M05 开发）
- [x] **M07 商家记忆与相似堆**（前置：M05、M06；后端建议；可与前端任务独立）
- [x] **M08 分组内容接入现有审核**（前置：M03、M04、M06；后端审核桥接；不依赖 M07）
- [x] **M09 前端混合上传与批次恢复**（前置：M03、M04；可与 M05—M08 的后端开发独立）
- [x] **M10 前端卡片整理页与移动端操作**（前置：M06、M09；可用固定候选先开发，不依赖 M07）
- [x] **M11 前端财务表格视图**（前置：M06、M09；与 M10 共用 API，可独立开发）
- [x] **M12 前后端闭环与现有样本验收**（前置：M01—M11）

### M00：现有样本清单与基线

- [x] 盘点当前 `samples/real/`，按页/图片建立带文件校验值的私有清单；沿用已有真值并标明其来源与未复核字段。逐项补全实际采购日、买卖方和正确归组只在现有原件足以可靠判定且后续步骤需要时进行，不以补齐 83 项为 M00 完成条件。客户原件与逐项标注只放忽略目录。
- [x] 对旧响应做可重复回放，将主提示词未问 `supplier` 单列为 `not_asked`；纸面缺失、模型漏读、读错仅在有可核对真值与对应模型输出时统计。印刷扫描、手写、小票、数字 PDF 分层列出覆盖和未测范围，不把少数清晰件外推到全部图片。
- [x] 定义商家候选正确率、错误归组率、未归组率、每批人工操作次数和完成时间的统一口径；把同文件跨日期/跨商家、双语商家、买卖方并列、模糊日期、跨来源 CSV 列为后续合成回归场景，不要求仓库中已有对应客户原件。
- **验收**：可复查的现有样本清单、数据边界、回放命令、旧配置基线及明确的未测范围均已提供；M00 是开发基线，不代表已测得新提示词的商家准确率。后续步骤可直接开工。
- **验证记录（2026-11-21；按当日用户确认的材料边界调整 M00 范围）**：
  - 修改 `backend/tools/eval_multifiles_m00.py`、`backend/tests/test_eval_multifiles_m00.py`、本节及 `backend/docs/impl_record.md`；在被 Git 忽略的 `backend/samples/real/m00/` 生成 `manifest.json`（逐项清单与待复核标注）和 `baseline.json`（历史响应回放）。没有迁移、接口或运行时 OCR 改动。工作树原有改动均保留。
  - 样本清单实际覆盖 83 个内容单元：印刷扫描 6 页、中文手写扫描 68 页、零售小票 4 张、数字 PDF 5 页；现有逐字段旧真值仅覆盖印刷 6 页与小票 4 张。清单记录文件 SHA-256、原文件/页引用及待填的实际采购日、纸面日期、卖方英文/中文、买方、纸面是否有来源和正确归组。83 项均为 `pending_original_review`，旧 GT 原文仅作标注草稿，不能当作已复核 M00 真值。`samples/real/` 实查没有 CSV，也没有可识别为用户所说那十张典型 JPG 的整组材料；现有 4 张小票不能替代那组 JPG。
  - 实际命令（仓库根目录下先 `cd backend`）：`& .\.venv\Scripts\python.exe tools/eval_multifiles_m00.py inventory`；`... init`；`... replay --out samples/real/m00/baseline.json`；`& .\.venv\Scripts\python.exe -m pytest tests/test_eval_multifiles_m00.py -q`。结果：清单创建 83 项；回放两个模型各有 78 份旧响应，提示词 SHA 均为 `12e0246b0377`，共 156 次内容单元×模型的 `supplier` 为“未询问”（手写 136、印刷 12、小票 8）；数字 PDF 5 页×2 模型没有这一配置的旧 OCR 响应。测试 3 项通过。旧 GT 的 10 页中来源状态暂记 7 个 `recognized`、3 个 `unreadable`，尚未逐张复核，不能把后者直接解释成“纸面没有”。本轮是**历史结果回放**，没有真实 OCR 调用，也没有浏览器操作。
  - 后续统一计量口径：商家候选正确率＝已人工复核单元中正确的非空来源候选数 ÷ 可判真伪的非空候选数；同时报告纸面确有且可读来源的候选覆盖率。`not_asked`、无响应、真值未复核、纸面不可读的模型猜名分别单列，不进准确率分母。错误归组率＝自动放入日期/商家组且组不符人工真值的单元数 ÷ 自动放组且可核对的单元数；未归组率＝分类建议后仍缺日期或商家的可归组单元数 ÷ 全部可归组单元数。每批人工操作次数按用户发起的确认/移动命令计，多选一次记一操作并另记受影响单元数；整理耗时从处理完成可开始整理到用户完成该批整理，另报 OCR 等待时间。日期以实际采购日为分组真值，纸面原文单存；同文件跨组以页/行为单元。结果按印刷、手写、小票、数字 PDF 分层，兼报分子分母，不跨层外推。
  - 范围限制而非开发阻塞：83 项原件尚未按所有新字段逐项复核；现有材料没有十张典型 JPG、跨来源 CSV 等边界原件。旧提示词未问来源，不能量出新提示词的真实识别率；批次界面尚未开发，不能量操作数和耗时。M00 的现有样本基线已满足调整后的验收；M01、M02 可开工。缺失场景在对应功能步骤用合成回归验证，真实效果只在当前可核对样本上报告。

### M01：模型提取商家

- [x] 修改 `src/integrations/dashscope.py` 的内容提示词与输出解析：要求区分卖方/商家与买方，分别读纸面英文、中文商家名；保留原始两种写法，由后端按「有纸面英文取英文，否则取中文」产生 `source_name` 候选。不翻译、不臆造、不把未知写成某个商家。
- [x] 将商家原文、模型分歧、页码和可得证据接入现有识别契约、草稿证据和响应；英文/中文取舍只影响展示及规范化来源，原文不被覆盖。两模型冲突与“纸面没有”要能区分。
- [x] 更新提示词版本/SHA、依赖旧 SHA 的测试和评测工具；旧评测结果保留为历史配置。在当前可核对原件上重跑新配置并检查日期、明细数值和行匹配是否退化；没有 OCR 凭据时用合成响应验证契约与回归，并明确记载真实新配置准确率未测。缺凭据不阻断已实现的商家识别功能交付，不得将旧 SHA 的结果算作新配置成绩。
- **验收**：合成契约测试覆盖英文+中文同印时选纸面英文、仅中文时选中文、买方英文不能冒充卖方、无来源时保持未知；现有原件能核对的情形补真实样本验证。旧准确率不会被标成新配置的结果，未测的真实准确率明确标空。
- **验证记录（2026-11-21；用户审核后已勾选 M01 总项及三个子项）**：
  - 修改 `backend/src/contracts.py`、`backend/src/integrations/dashscope.py`、`backend/src/integrations/crosscheck.py`、`backend/src/services/extraction.py`、`backend/tools/eval_adapter.py`、`backend/tools/eval_multifiles_m00.py`、新增 `backend/tools/eval_multifiles_m01.py`、`backend/tests/test_ocr_adapter.py`、`backend/tests/test_extraction_service.py`、`backend/tests/test_eval_multifiles_m00.py`、`frontend/src/features/invoices/evidence.ts`、`frontend/src/features/invoices/InvoiceHeaderEditor.vue`、`backend/README.md`、本文件和 `backend/docs/impl_record.md`。在现有未提交工作树上增量修改；无迁移、无新增 URL。`GET /api/invoices/{id}` 的现有 `recognition.raw_document_fields.fields` 增加 `supplier_english`、`supplier_chinese`、`supplier_evidence` 及裁决证据，`source_name` 仍是待核对候选。
  - 提示词新 SHA 为 `67fb7df1217b`；步骤 07 的旧提示词和既有 156 份来源未询问的历史响应仍属于 `12e0246b0377`。M00 私有清单和旧回放文件实存于 `backend/samples/real/m00/`；本轮 `& .\.venv\Scripts\python.exe -m pytest tests/test_eval_multifiles_m00.py -q` 通过。合成测试覆盖双语优先英文、仅中文、买方英文不能代替卖方、纸面无来源与模型分歧、原文进入草稿和 API 输出。
  - 实际验证命令：在 `backend/` 运行 `& .\.venv\Scripts\python.exe -m pytest tests/test_ocr_adapter.py tests/test_extraction_service.py tests/test_eval_multifiles_m00.py tests/test_extraction_api.py -q`（当前工作树下 80 项通过）；`& .\.venv\Scripts\python.exe -m pytest -q`（最终重跑全套通过，退出码 0）；`$env:PYTHONIOENCODING='utf-8'; & .\.venv\Scripts\python.exe tools/eval_adapter.py replay --out samples/real/m00/m01_legacy_adapter_replay.json`（通过，报告及日志在被忽略目录）。在 `frontend/` 运行 `npm run typecheck`、`npm run build:h5`（均通过）。`git diff --check` 退出码 0。完整测试中途曾因同一工作树的 M02 新表与旧表清单测试不同步而失败两项；相关测试更新后重跑全绿，本轮未改 M02 代码。首次回放因 Windows 控制台 GBK 无法输出警告符号而在打印阶段报 `UnicodeEncodeError`，设 `PYTHONIOENCODING=utf-8` 后完整通过。
  - **历史结果回放**：覆盖旧 GT 的 6 张印刷页与 4 张零售小票，并运行旧响应的空白格、行容量、税率列误读检查；本地数字 PDF 文字层 5 页解析出 29 行（无 OCR 调用）。旧配置印刷字段预填精度 80/82、正确留空 6、错 2，报告 SHA 是 `12e0246b0377`。这些是旧配置回归基线，不能写成新提示词成绩。
  - **用户明确授权后的真实 OCR**：在 `backend/` 运行 `$env:PYTHONIOENCODING='utf-8'; & .\.venv\Scripts\python.exe tools/eval_adapter.py live --out samples/real/m00/m01_live_adapter.json`，工具在 `live` 模式读取 `.env`，报告 SHA `67fb7df1217b`。对现有旧 GT 的 6 张印刷页和 4 张小票真调两模型；六项检查均通过，印刷字段预填精度仍为 80/82（97.6%），日期 7/9、总额 10/10 与旧回放相同，行容量、空白格、税率列误读检查通过。**具体退化**：08 号小票的 `unit` 税码误报由旧响应 3 处增至新响应 14 处；现有小票路径不预填字段，这些误报只留在原始证据，不能宣称识别没有退化。报告和日志只在被忽略的 `samples/real/m00/`。
  - **商家真实样本核对**：人工查看当前两张中英双语印刷页与两张顶部未见商家名的小票；运行 `tools/eval_multifiles_m01.py --sample <当前原件路径> ... --out samples/real/m00/m01_supplier_spot.json`（实际 4 个 `--sample` 路径和 SHA 留在私有报告），两模型对 4 张真调，原始响应只存私有报告。两张双语页均读出纸面英文卖方并排除纸面买方；两张小票没有编造来源候选。两张印刷页的商品行数分别与纸面 1 行、2 行相符；未对全部 83 单元做逐行匹配真值。第 03 号页的中文名仅有末尾句点差异，商家名称比较忽略首尾标点但保留两个原文；`tools/eval_multifiles_m01.py --rejudge samples/real/m00/m01_supplier_spot.json --out samples/real/m00/m01_supplier_spot_rejudged.json` **离线重裁决**后，两张双语页均给待核对英文候选，两张小票仍为未知。这是 4 张人工可核对样本的行为证据，不外推为全量商家准确率。另对 08 号小票试跑一版加税码规则的提示词（SHA `4a36218bbf36`），主模型仍报 14 处税码单位；该试验版已撤回，不能与终版报告合并。
  - **限制**：最初外发命令被自动审批拒绝；用户随后明确授权把当前原件发往 `.env` 配置的阿里云 DashScope 主机后才运行，没有绕过拒绝。M00 清单 83 单元尚未逐项复核卖方真值；除上述 4 张 spot 外，新配置商家候选准确率、覆盖率均未测。前端只完成类型检查和 H5 构建，未做实际浏览器操作。现有样本不含十张典型 JPG 或跨来源 CSV，不以补样作为 M01 开发前置。

### M02：整理批次的数据与存储边界

- [x] 设计并迁移持久化的批次、原文件引用、可归类内容及其来源页/表格行、分类候选、人工决定与版本。日期/商家组由决定值派生，不另存一份会与内容打架的「组真相」。
- [x] 明确原件只存一次、一份 PDF 可被多个组引用的关系；审查当前 `invoice_files.sha256` 全局唯一和「文件只属于一张单据」的限制，给出不破坏旧数据的迁移/兼容方式。批次文件重复、上传中断与文件回收要有明确状态。
- [x] 建立 Service/Repository 的最小读写原语与隔离数据库测试；此步不接真实 OCR、不做完整上传界面，允许用人工内容验证迁移与引用关系。
- **验收**：同一个文件的第 1 页和第 3 页可持久化为两个独立分类内容、各自回指原页；旧发票/旧导入批次迁移后可照常回读；失败事务不留下半套组关系。
- **验证记录（2026-11-21；用户已审核通过，总项与三个子项已勾选）**：
  - 修改/新增 `backend/src/models.py`、`backend/migrations/versions/20261121_1400_a3c81d57e240_add_intake_batches.py`（新增）、`backend/src/repositories/intake.py`（新增）、`backend/src/services/intake.py`（新增）、`backend/tests/test_intake_service.py`（新增）、`backend/tests/conftest.py`、`backend/tests/test_migrations.py`、`backend/tests/test_db.py`、`backend/src/repositories/__init__.py`、`backend/src/services/__init__.py`、`backend/docs/struct.md`、`backend/README.md`、本节与 `backend/docs/impl_record.md`。在现有未提交工作树上增量修改，M00／M01 的改动均保留。**没有新增 URL、没有前端改动**：批次与上传接口是 M03/M04 的范围。
  - 迁移 `a3c81d57e240`（前一修订 `f31a7e9c4b20`）**只加五张表、不改任何既有表**：`intake_batches`、`intake_files`、`intake_batch_files`、`intake_items`、`intake_item_candidates`。⚠️ **没有「日期/商家组」表**——组是 `(decided_purchase_date, decided_source_name_normalized)` 的 `GROUP BY` 结果，由内容上的人工决定值派生。⚠️ **`invoices` / `invoice_files` 一个字都没动**：那边 `sha256` 全局唯一是步骤 05「完全相同的文件不许重复上传」的落点、`invoice_id` 非空意味着一份文件只属一张单据；整理阶段要的方向相反，所以两套表并存，跨路径的同摘要文件由 `register_file` 报出旧单据 id **只提示不阻断**（被否决的放宽方案见 `impl_record.md`）。
  - 原件只存一次：`intake_files` 按 `sha256` 与 `storage_key` 双唯一，一行一份字节；批次对它的引用在 `intake_batch_files`，`(batch_id, intake_file_id)` 唯一（同批次重复文件 409）。文件状态四态明确：`pending` 等处理、`ready` 已切分、`failed` 解析失败、`discarded` 已回收；回收保留行与内容并由 `active_reference_count` 回答「字节是否还被别的批次引用」，**本步骤不删磁盘文件**。中断的上传不会留下 `intake_files` 行（摘要要等字节收完才算得出），表现为「文件不在批次里」，重传即可。
  - 实际命令（在 `backend/`）：`& .\.venv\Scripts\python.exe -m alembic -x db_url=sqlite:///var/m02-check.db upgrade head`（通过）→ `compare_metadata` 比对模型与库，差异为 `[]` → `downgrade -1`（五张表干净删除，其余 12 张业务表不变）→ 再 `upgrade head`（通过），临时库已删除。`& .\.venv\Scripts\python.exe -m pytest tests/test_intake_service.py -q`：**38 项通过**。`& .\.venv\Scripts\python.exe -m pytest`：**1276 项通过，退出码 0**（含 `test_models_match_migrations`、`test_upgrade_creates_exactly_the_expected_tables`、`test_declared_business_tables_are_exactly_the_expected_ones`）。`git diff --check` 退出码 0。
  - 三条验收逐项有用例：①「五页 PDF 的第 1 页与第 3 页各是一个内容、各自回指自己那一页」并可落入 11/8 与 11/9 两个不同组，第 2 页仍在待归类组；②旧发票与旧 `entry_source='import'` 批次在同一个库里照常回读，且旧的全局重复上传 409 仍然生效；③切页时重复页号触发失败后**一个内容都不留、文件退回 `pending`、`item_count` 仍为 NULL**，数据库侧另有两条局部唯一索引与三条 CHECK（页/行坐标不许混填、来源展示名与规范化名成对）兜底。另覆盖：拖到日期标题不会抹掉已确认商家（`UNSET` 与 `null` 分开）、`Costco` 与 `COSTCO` 不分成两组、乐观锁拦住并发移动而重复同一命令不涨版本、歧义日期文本 `11/08/26` 拒绝而不猜、**候选永远不会自己变成决定**、按依据整组替换候选、批次状态派生自两个时刻、大表读取分页。
  - **本轮是迁移实跑 + 隔离数据库的合成内容测试，没有真实 OCR、没有文件上传、没有浏览器操作**。测试库由真实 `alembic upgrade head` 建出（`migrated_database` 夹具），不是 `create_all`。文件字节是人工构造的摘要与存储引用（`synthetic/...`），商家名一律是 `Example Seller`、`Costco` 等合成或公开字符串，没有引用 `samples/real/` 的客户原件。
  - 未覆盖/未测范围：①**没有任何 HTTP 接口**——批次创建/恢复、逐文件上传、处理状态与回看原件的 API 在 M03/M04；②真实 PDF 分页、HEIC 解码、CSV/XLSX 行解析一行都没接，`record_items` 的页号/行号由调用方给出；③批量移动与撤销栈留给 M06，本步只有单内容分类命令；④候选的生成（日期解析、商家建议、相似堆）留给 M05/M07，本步只有写入原语；⑤组内容送进现有审核留给 M08，因此「同组几张真实发票如何各自收口」尚未验证；⑥`register_file` 的跨路径提示只在合成字节上验过，真实客户原件同时走两条路径的情形未测。

### M03：图片/PDF 的批次摄入

- [x] 增加创建/恢复批次、逐文件上传、处理状态与回看原件的 API；使用现有本地文件适配器、格式白名单和大小校验。图片按张、PDF 按页生成稳定的可归类内容 ID；多页 PDF 不在上传时直接等同一张发票。
- [x] 文件解析失败、某页失败、重复字节和中断后重试均返回可恢复状态；已经成功保存的文件/页不丢。后台任务和浏览器生命周期解耦，状态可轮询，但不显示假百分比。
- [x] 提供可注入的处理器供 M05 使用；此步只需确定性页数、原件引用、预览数据和任务状态，不提前决定商家或日期。
- **验收**：一份五页 PDF 产生五个可回看的页引用；第 2 个文件上传失败时第 1 个文件和批次仍可恢复；旧单张上传 API 仍通过回归。
- **验证记录（2026-11-21）**：
  - 修改/新增 `backend/src/services/intake_ingest.py`（新增）、`backend/src/api/intake.py`（新增）、`backend/tests/test_intake_ingest.py`（新增）、`backend/tests/test_intake_api.py`（新增）、`backend/src/services/intake.py`、`backend/src/repositories/intake.py`、`backend/src/documents/files.py`、`backend/src/services/invoices.py`、`backend/src/api/schemas.py`、`backend/src/api/__init__.py`、`backend/src/factory.py`、`backend/src/worker.py`、`backend/app.py`、`backend/tests/conftest.py`、`backend/tests/test_entry.py`、`backend/tests/test_health.py`、`backend/docs/struct.md`、`backend/README.md`、本节与 `backend/docs/impl_record.md`。在现有未提交工作树上增量修改，M00—M02 与并行开发的 M04 改动全部保留；**没有改 M04 的任何文件**（`services/intake_spreadsheets.py`、`repositories/intake_spreadsheets.py`、`tests/test_intake_spreadsheets.py`、其迁移与 `api/spreadsheets.py` 均未动）。
  - ⚠️ **没有新迁移**。切页的待办状态用 M02 已有的 `intake_batch_files.status`——`pending` 的含义就是「字节存好了、还没切」，`status` 本身就是队列。另立一张任务表会出现「任务表说跑完了、文件还是 pending」两份能互相矛盾的真相。`extraction_jobs` 需要独立任务表是因为那边有 `running` 中间态、重跑要再花一次钱，本地切页两者都没有。
  - 新增 9 条 URL，前缀对齐 M04 已经在用的 `/api/intake-batches`（同一个批次只有一种写法）：`POST /api/intake-batches`、`GET /api/intake-batches/current`（没有就 404，前端据此决定"接着整理"还是"新建一批"）、`GET /api/intake-batches/<id>`（轮询处理状态）、`GET .../items`、`POST .../files`（**一次一个文件**，字段 `file`）、`POST .../process`（恢复/重试队列）、`DELETE .../files/<id>`、`GET .../files/<id>/original`、`GET .../items/<id>/original`。⚠️ **状态只有 `pending` / `ready` / `failed` / `discarded` 四个离散值加「切出了几个内容」，没有百分比**——本地切页没有可信的中间进度。
  - 分层：`services/intake_ingest.py` 只做「字节 + 页」，落库一律经 M02 的 `register_file` / `record_items` / `fail_file`，自己不开 Session，于是「切到一半失败不留半套关系」那条事务保证继续由 M02 提供；页数由 `documents/` 本地算（数 PDF 页树、解图验坏并量尺寸），一行代码不访问网络。白名单沿用步骤 05 的 `ALLOWED_UPLOAD_EXTENSIONS`（把 `_extension` 改名为公开的 `upload_extension` 共用，不另抄一份）；**收不收按扩展名，怎么切按字节**——`.jpg` 里装着三页 PDF 会被切成三页。
  - **自动化测试**：新增 Service 层 26 项、HTTP 层 16 项。实际命令（在 `backend/`）：`& .\.venv\Scripts\python.exe -m pytest tests/test_intake_ingest.py tests/test_intake_api.py -q`（42 项通过）；`& .\.venv\Scripts\python.exe -m pytest`：**1324 项通过，退出码 0**（M02 基线 1276 + 本步 42 + M04 的用例）。`git diff --check` 退出码 0。
  - **真实原件实跑（纯本地计算，没有任何外部调用、没有 OCR、不花钱）**：`samples/real/` 的 10 份原件走完整摄入通路 → **83 个可归类内容**，与 M00 清单独立数出来的 83 个内容单元一致。页数与文件名标注逐项吻合（32p→32、18p→18、1p→1、**5p→5**、18p→18），4 张图片各 1 页；盘上恰好 10 份字节（原件只存一次）；**83 页逐页回看，SHA-256 与原文件完全一致**；83 页全部留在「待归类」一个组里，`decided_*` 全空、候选 0 条（摄入不替人决定）。数字 PDF 5 页报出有文字层、74 页扫描页报出有整页图，与 M01 记录的「本地数字 PDF 文字层 5 页」对得上——这正是 M05 分流要用的事实，不必把同一份 PDF 再解析一遍。报告在被忽略的 `samples/real/m03/m03_real_ingest.json`。
  - **真实本机服务器实跑**（`python app.py` + 临时库，`OCR_PROVIDER=fake`，用完已删）：**验收第一条**——真实 5 页 PDF 上传得到 `queued=true` / `pending` / `ingest=null`，轮询后 `ready` + `item_count=5`，5 个页引用逐个 `GET .../items/<id>/original` 返回 200、`X-Intake-Page` 为 1—5、字节与原文件 SHA 一致。12 MB / 32 页真实 PDF 的上传请求 **0.07 秒返回、0.30 秒后 ready**——切页确实在请求之外，浏览器关掉不影响已经收下的字节。**中断后重试**：用另一个进程在同一个库/存储上留下一条没人管的 `pending`（与进程被 kill 后剩下的状态一样），服务器 `POST .../process` 把它切成 18 页并逐页可回看。真实 PDF 的截断副本得到 201 + `failed` + `pdf_unreadable`「PDF 打不开：Stream has ended unexpectedly」，**同批次其它文件状态不受影响**。跨批次 id 读原件 404、同批次 200；同一批次重复文件 409；CSV 400 并给出受理扩展名。
  - **验收第二条**（第 2 个文件失败不连累第 1 个）有 Service 与 HTTP 两层用例：第 1 个文件 `ready`、第 2 个超限 400、批次仍 `open` 且页数不变、接着传第 3 个照样成功。这是**结构性**的——一次一个文件，不是靠补偿逻辑。**验收第三条**（旧单张上传回归）：全套 1324 项含步骤 05/06/08 的既有用例；另有专门用例验同一份字节在旧路径已有单据时**只提示不阻断**（`same_bytes_invoice_ids`），而旧路径自己的全局重复 409 仍然有效。
  - **开发中发现并修掉的两个真问题**：①上传响应里的 `file` 用的是登记那一刻的快照，当场切完时仍报 `pending`——改成以现状汇报；②页已切好、之后被别处标成 `failed` 的文件重试会撞 `(batch_file_id, page_no)` 唯一索引变成 500——改成按 `item_count` 判定「已经切过」，返回 `already_ingested` 且页的 id 不变。两者都有专门用例。
  - **未测/未覆盖**：①**没有真实 HEIC 原件**（`samples/real/` 一张都没有），缺解码器时「照样登记一页 + 留一条 note」只在合成路径验过；②摄入阶段不产生任何日期/商家候选，M05 的分流效果与 M06 的归组操作在这一步无从验证；③可注入处理器只用测试替身验过契约；④没有前端与浏览器操作（M09/M10）；⑤**「某页失败」在本步收敛成「整份文件失败」**——pypdf 的页枚举是全有或全无，逐页的内嵌图列举失败已收进 notes，真正的逐页成败要等 M05 的逐页识别；notes 是本次调用的返回值与日志，**没有落库**（落库要加列，本步不加迁移）；⑥并发只验到「重复驱动队列安全」，多进程同时切同一个文件没有压测；⑦恢复队列靠显式的 `POST .../process`，**启动时不扫库**（那会破坏 README 第 10 节守着的「启动不连数据库」）。

### M04：CSV/XLSX 的批次摄入

- [x] 复用现有表格预览、列映射、日期/数字格式及错误口径；CSV/XLSX 按行形成可归类内容，保留原表格行号和单元格原文，不把整份表强行归到一个日期或商家。
- [x] 跨日期/跨来源的行可分别分组；某行日期或商家缺失时只让该行待处理。大表在 API 中分页/汇总，不为 5000 行一次返回 5000 张大卡片；不经图片 OCR。
- [x] 处理旧导入接口与新批次入口的兼容及重复文件提示，不能因相同 CSV 被重新上传而静默生成第二批采购记录。
- **验收**：用明确标为合成的 CSV（两个日期、两个商家和一行缺来源）验证四个可辨的组/待办状态；原行号可回查；现有单文件导入路径仍可用。不宣称已有真实跨来源 CSV 覆盖。
- **验证记录（2026-11-21）**：
  - 修改 `backend/src/models.py`、`backend/src/services/spreadsheets.py`、`backend/src/api/spreadsheets.py`；新增迁移 `b91e6a74d320`、`backend/src/repositories/intake_spreadsheets.py`、`backend/src/services/intake_spreadsheets.py`、`backend/tests/test_intake_spreadsheets.py`；更新 `backend/README.md`、`backend/docs/struct.md` 与 `backend/docs/impl_record.md`。M03 由另一开发者并行处理；本步未改其图片/PDF 处理代码或测试。
  - 迁移仅给 `intake_batch_files` 加 `table_metadata`、给 `intake_items` 加 `row_data`，均可空，旧行与旧导入结构不变。新入口复用步骤 06 的解析器、列映射和保守日期/数字规则；每条非空原始表格行存自己的行号、解析值、所有单元格原文、逐行问题。表格原件经现有存储适配器保存，重复字节复用已有原件。人工决定与系统候选继续分离；M04 不自动确认日期/商家，M05 再接结构化候选，M08 再送现有审核。
  - 新增 `POST /api/intake-batches/<id>/spreadsheets/preview`（分页预览）、`POST /api/intake-batches/<id>/spreadsheets`（上传）、`GET /api/intake-batches/<id>/spreadsheets/rows`（分页，默认 50、最多 200）、`GET .../rows/<item_id>`、`GET .../groups`、`GET .../<batch_file_id>` 和 `GET .../<batch_file_id>/original`。创建/恢复批次由 M03 的通用接口负责；M04 接现有 M02 批次 ID。旧 `/api/imports` 与 `/api/imports/preview` 保持原契约。跨批次或旧导入同字节先返回 409 与关联 ID，只有显式 `confirm_duplicate=true` 才加入；同批次重复仍 409。本步不生成任何采购记录。
  - 实际运行：从仓库根目录用提升执行方式运行 `Get-Location` 成功；默认执行器在命令启动前反复报 `helper_unknown_error: setup refresh had errors`。`compileall` 通过；`pytest -c backend/pytest.ini -o addopts='' backend/tests/test_intake_spreadsheets.py backend/tests/test_spreadsheets.py backend/tests/test_intake_service.py backend/tests/test_migrations.py backend/tests/test_db.py -q`（`PYTHONPATH=backend`，使用 `backend/.venv/Scripts/python.exe`）**168 passed in 6.21s**；`git diff --check` 退出码 0（只有 CRLF 提示）。一次并行 M03 写入过程中曾遇到 `MAX_WAITING_FILES` 尚未定义的导入失败；待该模块恢复完整后重跑通过，未修改 M03。
  - 合成 CSV 的四行分别给出两日两商家及一行缺商家，按 M02 人工分类命令形成四个组；原行号 2—5、单元格原文、缺值和原件字节均可回查。合成 XLSX 验证列映射、分页预览、歧义日期只影响该行；5000 行含表头的合成 CSV 验证返回页仅 50 行、超过 200 的请求被拒。另验证跨批次/旧导入重复提示、显式确认、写行失败后文件为 `failed` 且可重试、旧导入仍可创建。所有这些是**合成夹具与隔离数据库/API 测试**，没有运行真实 OCR、真实跨来源 CSV 或浏览器操作；仓库现有真实样本没有跨来源表格，故其归组准确性未测。

### M05：分类候选与日期解析

- [x] 为图片/PDF 内容运行日期和商家元数据提取，保存原文、候选值、出处与处理状态；数字 PDF 先用文字层，扫描件走新模型配置，失败时保持待定。整理阶段已有结果进入审核时不得再次为同一页重复收费识别。
- [x] 没有人工确认商家时，不能仅凭相似度应用该商家的 OCR 路由；记录模糊日期的两种可能（如 `6/11/2026`），不按上传日或系统地区静默猜。商家确认后，用保存的日期原文和该商家的 `date_order` 重新解析，无需重新 OCR。
- [x] 接入 M04 的结构化表格行候选：表格中明确的日期/商家优先用原单元格证据；识别失败不影响其他行。
- **验收**：商家未知且日期歧义时两项可独立待定；随后确认商家并设 `date_order` 可重新得到日期；重新打开批次不重复调用模型，提取失败不丢原件。
- **验证记录（2026-11-21 开发 / 2026-11-22 真实 OCR 抽样）**：
  - 新增 `backend/src/services/intake_recognition.py`、迁移 `20261121_1800_c47be2a91f85_intake_item_recognition.py`、`backend/tests/test_intake_recognition.py`、`backend/tests/test_intake_recognition_api.py`、`backend/tools/eval_multifiles_m05.py`；修改 `backend/src/models.py`、`backend/src/normalization.py`、`backend/src/services/intake.py`、`backend/src/repositories/intake.py`、`backend/src/api/intake.py`、`backend/src/api/schemas.py`、`backend/src/factory.py`、`backend/src/services/__init__.py`、`backend/tests/conftest.py`、`backend/tests/test_normalization.py`、`backend/tests/test_entry.py`、`backend/tests/test_health.py`、`backend/docs/struct.md`、`backend/README.md`、本节与 `backend/docs/impl_record.md`。在现有未提交工作树上增量修改，M00—M04 的改动全部保留；**没有改 M04 的表格解析代码**，也没有碰并行开发中的 M06 文件。
  - 迁移 `c47be2a91f85`（前一修订 `b91e6a74d320`）**只给 `intake_items` 加五列、不加表、不改既有列**：`recognition_status`、`recognized_at`、`recognition_payload`、`recognition_error_code`、`recognition_error_message`，外加 `recognition_status` 的索引。⚠️ **没有「识别任务表」**——与 M03「切页的待办状态就是 `intake_batch_files.status`」同一条：`recognition_status` 本身就是队列。⚠️ 这一列**刻意不加 CHECK**：SQLite 改 CHECK 必须批处理重建整张表，而 `intake_items` 上有两条带 `WHERE` 的局部唯一索引（`uq_intake_items_page` / `uq_intake_items_row`），重建时丢掉 `WHERE` 的代价远大于一条 CHECK；实跑后已逐条核对这两个索引的 SQL 仍带 `WHERE`。
  - 新增 2 条 URL：`POST /api/intake-batches/<id>/recognize`（驱动识别队列，`retry_failed` 可选）、`POST /api/intake-batches/<id>/items/<item_id>/reparse-date`（用保存的原文按已确认商家重解析）。`GET .../items` 的每张卡片多一个 `recognition` 块（状态、provider、模型、提示词 SHA、日期原文、两种读法、错误）。**确认商家没有 HTTP 入口**——分类命令属于 M06，所以本步不提前造一个会返工的接口，相关用例用 M02 的 Service 写决定值。
  - **分流按文件结构，不按商家**：PDF 有文字层 → 抽文字层，**一次模型都不调**；没有文字层 → 取整页图送识别器；图片 → 解码后送识别器；CSV/XLSX 行 → 读 M04 已存的单元格，**不经图像 OCR**。⚠️ `date_order` 与 `ocr_routes` **只认 `decided_source_name_normalized`（人工确认过的商家）**，候选里的商家名一个字都不参与——有专门用例：这家的 `date_order=mdy` 已设好、候选里也确实是这家，日期仍然保持两条读法。
  - **自动化测试**：Service 层 29 项、HTTP 层 10 项、`normalization` 新增 9 项。实际命令（在 `backend/`）：`& .\.venv\Scripts\python.exe -m pytest tests/test_intake_recognition.py tests/test_intake_recognition_api.py tests/test_normalization.py tests/test_migrations.py tests/test_intake_service.py tests/test_intake_ingest.py tests/test_intake_api.py tests/test_intake_spreadsheets.py` → **196 passed**；`& .\.venv\Scripts\python.exe -m pytest` → 本步完成时 **1372 passed，退出码 0**。迁移实跑：`upgrade head` → `compare_metadata` 差异为 `[]` → `downgrade -1`（五列干净删除、两条局部唯一索引的 `WHERE` 仍在）→ 再 `upgrade head`。`git diff --check` 退出码 0。⚠️ 之后并行开发的 M06 把它自己的两张表与四条路由加进了同一个工作树，于是最后一次全量跑是 **1374 passed / 3 failed**；那 3 项全部是 M06 的清单型断言（`test_db.py` 的业务表清单、`test_entry.py` 与 `test_health.py` 的路由清单里多出 `intake_moves`、`intake_move_entries`、`/moves`、`/moves/<id>/undo`、`/groups`、`/groups/items`），**断言里没有一项提到 M05 的产物**，本轮也没有改 M06 的任何文件——把它们登记成已完成是 M06 的交付内容。M05 相关的 196 项在同一棵树上仍然全绿。
  - **真实原件实跑（本地文字层，零成本、零外发）**：`& .\.venv\Scripts\python.exe tools/eval_multifiles_m05.py local --out samples/real/m05/m05_local_textlayer.json --confirm-source <纸面读到的卖方名>`。`samples/real/` 的 10 份原件 → **83 个可归类内容**（与 M00/M03 独立数出的 83 一致），**全程 0 次模型调用**。其中真实数字 PDF 的 5 页走文字层拿到日期候选，**5 页的纸面日期全部是歧义写法**（`6/11/2026` ×3、`6/10/2026`、`6/9/2026`），各留两条读法；另外 78 页被「拒绝被调用」的守卫挡下（`guard_attempts=78`），这正是「需要 OCR」的清单。**再驱动一次队列：0 条待办、0 次调用**。
  - **真实原件上的验收条件 2（零成本）**：对那 5 页确认商家（纸面读到的卖方名）并把这家的 `date_order` 设成 `mdy` 之后重新解析 → 每页**恰好一条**日期候选（`2026-06-11` ×3、`2026-06-10`、`2026-06-09`），**`reparse_model_calls=0`**，而 `decided_purchase_date` 仍然是 `null`（候选没有变成决定）。
  - **真实本机服务器实跑**（`python app.py` + 临时库，`OCR_PROVIDER=fake`，用完保留在被忽略的 `var/`）：上传真实 5 页数字 PDF → 5 页 `pending`（M03 的处理器钩子排队，排队本身不调模型）→ `POST .../recognize` 得到 5 条 `recognized` / `text-layer` / `model_calls=0`、日期两读法俱在 → **再 POST 一次：`results` 为空、`model_calls` 为 0** → `PUT /api/ocr-routes` 设 `date_order=mdy`、确认商家后逐页 `POST .../reparse-date` → 每页一条日期、`model_calls=0`、`decided_purchase_date` 仍为 `null`。
  - **用户授权后的真实 OCR 抽样（2026-11-22，10 页，20 次调用）**：用户在「全量 78 页 / 10 页抽样 / 不外发」中选了**10 页抽样**。命令 `tools/eval_multifiles_m05.py live --keep-db --out samples/real/m05/m05_live_sample10.json --include ...`，提示词 SHA `67fb7df1217b`，两模型交叉裁决。结果：**5/10 读出商家候选**（4 家印刷体抬头，其中同一份 PDF 的第 1 页与第 7 页读出同一家）；**ChangShun 手写件与 4 张照片一条商家候选都没有**（`unreadable` 或两模型分歧 → 不编造名字，留在「商家待定」）。**4/10 读出日期原文，其中 3 条是歧义写法**（`6/1/26`、`6/3/2026`、`09-02-22`）各留两条读法，1 条（`06/26/2026`，26 > 12）文本本身定死 → 一条候选。识别请求里 `original_name=None`，所以商家名来自纸面而不是文件名。**对这 10 页再驱动一次：10 条全部 `already_recognized`、0 次调用**——「不对同一页重复收费」在**付费的真实数据**上验过。原始响应与逐项结果只在被忽略的 `samples/real/m05/`。
  - **未测/未覆盖**：①**真实 OCR 只覆盖 10 页**，另外 68 页扫描件未外发——商家候选的真实覆盖率与准确率**不能**由这 10 页外推到 83 个单元，也没有逐项人工真值可比对；②真实数字 PDF 的 5 页**读不出商家**：卖方名是页首一行**没有标签**的标题，而页上唯一带标签的一方是 `Bill to:` 的**买方**，所以这里宁可留空让人命名一次，也不取页首标题——跨批次复用那个名字是 M07 的商家记忆，不是本步；③同文件参考、历史别名、相似版式堆（第 3.2 节的第 2—4 层）**一层都没做**，本步只有第 1 层「纸面直接读出」；④`recognition_payload` 里的行项目是**给 M08 复用的**，本步没有建任何草稿，所以「不重复收费」在审核那一端尚未端到端验证；⑤没有前端与浏览器操作；⑥HEIC 真实原件仍然一张都没有。

### M06：人工归组、批量移动与撤销

- [x] 提供读取「待归类／日期／日期下商家组」的分页 API；同一内容同时携带日期与来源的候选态、人工确认态和依据。没有商家时能落到某日期的「商家待定」。
- [x] 提供同一条批量移动命令支持：仅设日期、日期+商家一起设、从候选堆移出例外、清空错误决定；要求内容 ID + 版本或等价并发保护，重复请求不得重复建内容。
- [x] 持久化一次操作影响的内容集合，支持撤销最近一次移动；已进入确认/入账的内容不得仅改卡片归属，必须走 M08 的显式修改流程。
- **验收**：一次移动三张到 `11 月 8 日 / Costco`，只改这三张；拖到 `11 月 9 日` 标题则来源仍未知；撤销后全部恢复；双会话冲突不丢人工决定。
- **验证记录（2026-11-21）**：
  - 前置核对：M02 的五张表、单项分类和迁移 `a3c81d57e240` 在代码与迁移中存在；M03 的逐文件摄入、按页内容和原件回指接口存在。M02/M03 实施记录分别留有隔离迁移测试与真实原件本地摄入结果。本次还实际运行了它们的回归用例；M05 仍由另一开发者处理，本步没有运行其专属测试或改其识别规则。
  - 修改：新增 `intake_moves` / `intake_move_entries`、`intake_items.handed_off_at` / `pile_excluded` 与迁移 `d06a4c71e812`；新增独立的归组 Repository/Service、`GET /api/intake-batches/<id>/groups`、`GET .../groups/items`、`POST .../moves`、`POST .../moves/<move_id>/undo`。移动命令按 `request_key` 幂等，整批在一个事务内检查 ID、版本和审核交接状态；撤销只接受最近未撤销操作，并按版本保护。
  - 验证：从仓库根目录运行 `& .\backend\.venv\Scripts\python.exe -m pytest -c backend/pytest.ini -o addopts='' -q backend/tests/test_intake_moves.py backend/tests/test_intake_service.py backend/tests/test_intake_ingest.py backend/tests/test_intake_api.py backend/tests/test_intake_spreadsheets.py backend/tests/test_migrations.py`，**112 passed in 8.50s**，退出码 0。合成内容测试覆盖三张到 `2026-11-08 / Costco`、第四张不变、日期标题只定日期、撤销后恢复、双会话旧版本整批回滚、候选/决定/依据同屏响应、例外排除与恢复、已交审核的拦截；迁移测试在临时 SQLite 上真实升级。`git diff --check` 退出码 0（仅有换行符提示）。
  - 范围：这些是**合成内容与 HTTP 测试**；没有真实 OCR、历史识别结果回放或浏览器/手机操作。M08 接入正式审核时必须在同一事务设置 `handed_off_at`，并提供入账后显式纠错；当前尚无审核桥接。M07 的相似堆应读取 `pile_excluded`，本步只持久化人工排除决定，不生成相似堆。

### M07：商家记忆与相似堆

- [x] 从用户确认过的来源生成规范化名称和可更正别名候选；英文优先规则仍按纸面英文判断，不把中文自动翻译成英文。来源候选与商家主数据不是应付账款供应商模块。
- [x] 根据同文件已确认页与历史版式提出待确认的相似堆；记录每堆建议依据，允许用户一张张排除例外，再一次命名剩余内容。纸面明确冲突的来源不得被同文件或外观建议覆盖。
- [x] 在当前可核对样本上报告各线索的已测覆盖范围；用合成的同版式不同商家例外测试防止错归。缺真实跨商家真值时错归率标为未测；相似度阈值只用于候选排序，不自动把整堆确认到某商家。
- **验收**：一堆六张中排除一张例外后，一次给剩余五张赋来源；未来相似单据出现候选且能纠正；不同商家共用版式不会被静默合并。
- **验证记录（2026-11-22 开发 / 真实原件本地实跑 + 历史结果回放）**：
  - 新增 `backend/src/documents/layout.py`（版式指纹）、`backend/src/services/intake_suggestions.py`、`backend/src/repositories/intake_suggestions.py`、迁移 `20261122_1100_f2a9c35db714_intake_source_memory.py`、`backend/tests/test_intake_suggestions.py`、`backend/tests/test_intake_suggestions_api.py`、`backend/tools/eval_multifiles_m07.py`；修改 `backend/src/models.py`、`backend/src/api/intake.py`、`backend/src/api/schemas.py`、`backend/src/api/__init__.py`、`backend/src/factory.py`、`backend/src/services/intake_moves.py`、`backend/tests/conftest.py`、四处清单断言（`test_db.py` / `test_entry.py` / `test_health.py` / `test_migrations.py`）、`backend/docs/struct.md`、`backend/README.md`、本节与 `backend/docs/impl_record.md`。在现有未提交工作树上增量修改：**没有改 M05 的识别代码、M04 的表格解析，也没有碰并行开发中的 M08 审核桥接**（M08 当时正在同一棵树上写 `intake_reviews`）。
  - **三样「记忆」只有一样落表**。迁移 `f2a9c35db714`（前一修订 `e8b31f902a7d`，即当时 M08 的 head）只加一张 `intake_source_aliases`（`alias_key` 唯一、`source_key` 索引、`CHECK alias_key <> source_key`）与 `intake_items` 的两个可空列 `layout_fingerprint` / `layout_features` + 一条索引。⚠️ **没有供应商表，也没有「版式→商家」表**：「这一家现在叫什么」取最近一次人工确认的写法（整理决定 + 已入账采购记录聚合），「这个版式属于哪一家」由指纹加那一页上的确认派生——要更正就改那一页的确认，不会出现两份能互相矛盾的真相（与「日期/商家组不落表」同源）。只有别名映射是推不出来且必须可更正的人的决定（第 5 节），所以只有它落表。
  - 新增 6 条 URL：`POST /api/intake-batches/<id>/suggest-sources`（重算第 2—4 层建议）、`GET .../piles`（相似堆，**只读**）、`GET /api/source-memory`、`GET|POST /api/source-memory/aliases`、`DELETE /api/source-memory/aliases/<id>`。**整堆命名不另开写路径**：给 M06 的 `/moves` 加了一个 `action="source"`（只写来源、保留日期），因为堆里的卡片常常各属不同的一天，用 `group` 会逼着替它们猜一个共同日期。
  - **建议结构上花不起钱**：`IntakeSuggestionService` **没有识别器这个依赖**（有专门用例断言），三层线索全是本地计算与数据库查询，`model_calls` 恒为 0。
  - **指纹特征按实测选，不是拍脑袋**：扫描页/照片用「笔迹风格 + 格线行数 + 有无竖线 + 长宽比」——实测 `samples/real/` 的 5 份 PDF 抽 17 页，**行数在同一份单据里稳定、在不同商家之间不同**（16 / 17—18 / 25 / 2 / 4），而列数在同一份单据内就能从 3 跳到 5，所以列只保留「有没有」这一位；数字 PDF 用「页面尺寸 + 字号集合」——那份真实数字 PDF 的 5 页字号集合完全一致，而表头列位置的*个数*会随内容变（13 / 14 / 15），所以列位置只进证据、不进分组键。⚠️ **量不出格线的页标成 `layout_unknown`，那样的堆一条商家候选都不给**：两张都量不出格线的歪照片「指纹相同」不是证据。
  - **自动化测试**：Service 层 21 项、HTTP 层 9 项（含四条验收条件各自的用例）。实际命令（在 `backend/`）：`& .\.venv\Scripts\python.exe -m pytest tests/test_intake_suggestions.py tests/test_intake_suggestions_api.py` → **30 passed**；`& .\.venv\Scripts\python.exe -m pytest` → **1412 passed，退出码 0**（这一棵树上同时带着 M06 与 M08 的产物）。迁移实跑：`upgrade head` → `compare_metadata` 差异为 `[]` → `downgrade -1`（表与两列干净删除，`uq_intake_items_page` / `uq_intake_items_row` 的 `WHERE` 仍在）→ 再 `upgrade head`。
  - **真实原件实跑（本地测量，零成本、零外发）**：`& .\.venv\Scripts\python.exe tools/eval_multifiles_m07.py local --out samples/real/m07/m07_local_layers.json`。它复用 M05 那个**没花过钱**的工作库（先复制一份，原 `uploads/` 只读），对 `samples/real/` 的 **83 个可归类内容**全部量出版式：**35.7 秒、5 次驱动（每次 20 页）、0 次模型调用**。结果 **18 堆**：15 堆有模板证据、共 69 页，**每一堆都只来自一份原件**（按文件名代理，0 例跨文件混堆）；3 堆没有模板证据、共 9 页，其中两堆确实混了文件（ChangShun 5 页 + TangFa 1 页；两张照片各 1 页）——**这正是 `layout_unknown` 存在的理由，这三堆一条商家候选都不给**。代价是**过度细分**：ChangShun 的 32 页落进 4 堆、TangFa 的 18 页落进 7 堆、FengYuan 的 18 页落进 2 堆。方向是刻意的：多分几堆只多几次人工操作，混堆则会把来源记错。
  - **历史结果回放（零成本，不是新的真实识别）**：`tools/eval_multifiles_m07.py live --confirm-from-paper 2`。它读 M05 那次**用户授权过的 10 页真实 OCR**已经存下来的结果，模拟「老板接受纸面读出的商家」这一个动作（只确认 2 页，名字取自纸面读法，不编造）。结果：**同文件参考 17 条**（那份 18 页 PDF 的其余 17 页各拿到一条候选）、**版式相似 13 条**（`rows=18` 那一堆的 13 页，全部指向同一份文件已确认的那一家）、**历史写法 1 条**、冲突 0 条、`model_calls=0`；另外两家各自记下 **1 条 `paper_bilingual` 别名**——纸面上同时印着的中文写法，**不是翻译**（第 3.1 节禁止自行翻译）。3 页因为「纸面已经读出商家」不进堆：它已经有名字可接受，不需要靠外观。
  - **这次实跑改了一处实现**：第一版只给「需要进堆」的页量版式，于是真实回放里 `similar_layout=0`——因为纸面能读出商家的页**一上来就被确认**，永远拿不到指纹，版式记忆在最常见的那条路径上根本攒不起来。改成**已确认的页也量**（它们就是「历史版式」这条线索本身），同一次回放变成 13 条。专门用例按「先确认、后重算」的顺序锁住这条。
  - **真实本机服务器实跑**（`python app.py` + 临时库 `var/m07-live.db`、`OCR_PROVIDER=fake`，用完留在被忽略的 `var/`）：上传真实扫描 PDF `04_NewMoys_Meat_5p.pdf` + `03_Kunkee_Trading_1p.pdf` → 后台切成 6 页 → `POST .../suggest-sources` **2.9 秒量完 6 页、`model_calls=0`**，分出 3 个 `layout_template` 堆（`rows=2` ×4、`rows=3` ×1、`rows=4` ×1，与本地实跑对同样两份原件量出的指纹一致）→ `POST .../moves` `action=exclude` 排除 1 张，`GET .../piles` 那一堆从 4 变 3 且 `excluded_item_ids` 列出被排除的 id → 再一条 `action=source` 把剩下 3 张一次命名，**三张都只写了来源、`decided_purchase_date` 仍是 null** → `GET /api/source-memory` 列出这一家与确认次数 → `POST /api/source-memory/aliases` 记下别名（`basis=user`）、`DELETE` 撤销得 200 → **再驱动一次建议：`model_calls=0`、`layout_profiled=0`（量过的不重量）**，同一份文件剩下的那一页拿到一条 `same_file` / `0.8000` 候选，而它所在指纹没有已确认商家，所以**没有** `similar_layout` 候选 → 带旧版本号的 `action=source` 得 409。
  - **未测/未覆盖**：①**错归率未测**——仓库里没有逐项人工真值，上面用「原件文件名」做代理只能说明堆没有跨文件混，不能证明每一页的真实商家；②**真实的「同版式不同商家」样本没有**，那一条只在合成夹具上验过（两家已确认时两家**同时**作为候选摆出来，并带一条说明）；③真实数据上只模拟了 **2 次**确认，真实用户的确认与纠正行为未测；④**相似写法层在真实数据上产生了 1 条错候选**（分数 0.6254，按文件名代理指向另一家）——公司后缀（`Inc.` / `Market` / `Trading`）会抬高相似度；当前靠「排在最后一档 + 候选永不自动确认 + 纸面读法仍在卡片上」兜住，阈值要不要收紧留给 M12 用更多数据定；⑤同文件参考只在**同一个批次文件**内（跨批次的同一份字节走历史层，不算同文件）；⑥文字层页的模板特征只有一份真实数字 PDF，跨模板区分只在合成夹具上验过；⑦没有前端与浏览器操作（M10 / M11）；⑧HEIC 真实原件仍然一张都没有；⑨别名记忆只在调用 `/suggest-sources` 时从该批次的已确认页收集，没有后台扫全库。

### M08：分组内容接入现有审核

- [x] 将已整理内容幂等地转成现有发票/导入草稿与原件证据；**日期+商家组不是发票**。组内按单据号、页连续性等提出次级归属建议，允许用户在审核时合并/拆开页；同组多张发票的单号、总额、逐行确认和收口仍分别处理，不能生成一个合计的伪发票。
- [x] 组的已确认日期/来源作为草稿默认值，行级例外保留；审核中修改组分类时明确影响范围。已有采购入账后若要改日期/来源，使用现有采购修订与回读流程，不只改批次分组。
- [x] 同一页/行重复送审、浏览器重复点击、提取任务重试均不得重复生成采购；旧单张审核、旧表格导入及原件回看继续可用。
- **验收**：同日同商家两张发票各自收口，一份 PDF 跨五天的页进入对应五组；重复送审结果指回原草稿；已确认采购的日期和来源与历史记录一致。
- **验证记录（2026-11-22）**：
  - 前置核对：当前代码存在 M03 逐文件摄入与按页回指、M04 按行保存原单元格和解析值、M06 带版本批量归组与撤销；对应实施记录已有隔离迁移与 API 测试。本轮从仓库根目录实际运行包含它们与旧审核、表格、采购接口的定向回归，**210 passed in 15.46s**。M07 由另一 AI 同时编辑，M08 无业务依赖，保留其改动。
  - 变更：新增 `intake_review_links` 与迁移 `e8b31f902a7d`、`repositories/intake_reviews.py`、`services/intake_reviews.py`、`tests/test_intake_reviews.py`、真实样本离线脚本 `tools/eval_multifiles_m08.py`；扩展批次审核 API、单据详情的 `intake_sources`、提取重试/原件撤销保护及审核原件查看器。更新 `backend/README.md`、`backend/docs/struct.md`、`frontend/README.md` 与共享模型/路由清单测试。接口：`GET .../review-suggestions`、`POST .../reviews`、`POST .../reviews/<invoice_id>/reassign`、`PATCH .../reviews/<invoice_id>/classification`。原件仍只存于 `intake_files`，不放宽旧 `invoice_files.sha256` 唯一约束。
  - **合成测试**：`& .\backend\.venv\Scripts\python.exe -m pytest -c backend/pytest.ini -o addopts='' -q backend/tests/test_intake_reviews.py`，**5 passed in 1.15s**；真实迁移的临时 SQLite 上验证一份合成 PDF 五页进入五个日期组、同日同商家两张草稿分别逐行确认和收口、重发指回原草稿、连续页建议、审核中拆/并页、CSV 行入账、组分类修订同步已确认采购且留修改日志、行级覆盖保留、旧版本冲突、批次原件不可撤掉、重复提取返回 409。没有以这些合成结果推断 OCR 准确率。
  - **现有样本、离线非 OCR**：`& .\backend\.venv\Scripts\python.exe backend/tools/eval_multifiles_m08.py` 退出码 0；仓库的五页数字 PDF 原字节摄入、五页回指/原样回读、按纸面人工转录的 3 个日期和 5 个商家分别送成 5 张草稿、重发幂等，采购记录仍为 0。这份样本实际只有 3 个纸面日期；「同一 PDF 跨五天」由合成夹具验证，未冒称真实样本具有该边界。
  - **前端与总体**：`npm.cmd run typecheck` 退出码 0，`npm.cmd run build:h5` 退出码 0；这是类型/构建检查，未进行浏览器或手机实操。完整后端 `pytest -q backend/tests` 本轮首次跑得 **1398 passed、5 failed**：其中 3 项固定表/路由清单随后更新并单独复跑 **3 passed**，剩余 2 项在并行编辑的 M07 建议测试中（相似堆用例 `IndexError`、历史采购用例调用参数 `unit` 错误）。排除正在编辑的 `test_intake_suggestions.py` 后，`& .\backend\.venv\Scripts\python.exe -m pytest -c backend/pytest.ini -o addopts='' -q backend/tests --ignore=backend/tests/test_intake_suggestions.py` **1391 passed in 88.68s**；这不代表完整套件全绿。真实 OCR 模型与历史结果回放未运行，M09—M12 的混合上传/浏览器闭环仍待各自步骤。
  - 收尾修正：表格行明确生成 `entry_source=import` 草稿，页生成 `entry_source=invoice` 草稿，两类分别审核；待审与完成视图均可按行下载批次原始表格。修正后从根目录运行 `& .\backend\.venv\Scripts\python.exe -m pytest -q backend/tests/test_intake_reviews.py backend/tests/test_invoices_service.py backend/tests/test_invoices_api.py` 退出码 0，前端 `npm.cmd run typecheck` 与 `npm.cmd run build:h5` 再次退出码 0。一次未使用虚拟环境的 `python -m pytest ...` 命中 WindowsApps 别名，退出码 1、无测试输出，未作为验证结果。

### M09：前端混合上传与批次恢复

- [x] 调整 `frontend/src/pages/workspace/index.vue`、`features/invoices/InvoiceUpload.vue` 和平台文件选择/上传适配器：新增混合批次入口，可追加文件，选择后进入批次状态页；保留旧单张补传与手工路径。
- [x] 显示每个文件的真实上传进度、处理状态、失败原因与继续上传入口；刷新、返回列表和换设备可重新打开批次。不支持的格式/过大文件在本地先提示，服务端仍做最终校验。
- [x] CSV/XLSX 需要映射时进入现有预览能力，完成后返回同一批次；前端不把 PDF/JPG/CSV 混发到现有「一张发票」上传 API。
- **验收**：十二份混合文件中某一份失败，其余已上传文件可见；刷新后继续；CSV 映射错误可修正；旧单张入口回归正常。
- **验证记录（2026-11-22）**：
  - **前置与工作树**：开工先检查工作树，发现 M00—M08 与其它既有功能有大量未提交改动，均保留。M03/M04 的迁移、`/api/intake-batches` 图片/PDF 与表格入口、现有测试实际存在；隔离库升级到当前 Alembic head 成功，`backend/tests/test_intake_api.py` 与 `test_intake_spreadsheets.py` 本轮 **22 passed**。没有只凭勾选判断前置完成。默认 PowerShell 执行器在启动前报 `helper_unknown_error: setup refresh had errors`；按用户指定用 `sandbox_permissions=require_escalated` 重试只读 `Get-Location` 成功，后续必要命令沿用获准方式；没有把启动错误归因于中文路径。
  - **修改文件与契约**：新增 `frontend/src/features/invoices/IntakeBatchUpload.vue`；修改 `InvoiceUpload.vue`、`InvoiceList.vue`、`SpreadsheetImport.vue`、`frontend/src/pages/workspace/index.vue`、`frontend/src/shared/platform/filePicker.ts`、`upload.ts`、`workspaceNavigation.ts`、`frontend/src/shared/api/index.ts`、`types.ts`、`frontend/README.md`、本节及 `backend/docs/impl_record.md`。混合入口逐份传图片/PDF 到既有批次 `/files`，表格先走既有 `/spreadsheets/preview` 再走 `/spreadsheets`；批次详情轮询、`/current` 和 `?view=batch&batch=<id>` 负责恢复。没有后端接口、数据库迁移或 OCR 规则改动。单张 `/api/invoices` 与旧 `/api/imports` 流程保持独立。
  - **合成文件 + 实际浏览器**：隔离 SQLite 与上传目录、`OCR_PROVIDER=fake` 的 Flask、本地 H5 构建及 Edge 无头浏览器 CDP 实际点击。首批选 8 张不同 PNG、1 份合成五页 PDF、1 张内容损坏的 PNG、CSV、XLSX，共 **12 份**；原件 API 回读 11 份 `ready`、坏图 1 份 `failed`（`content_unsupported`），PDF 的 `item_count=5`，两份表格各 1 行。CSV 首次把商品列号设成 99，预览实际报「找不到字段 description 指定的列」；改回 1、重新预览并确认后，CSV 与 XLSX 都进同一批次。跨批次相同 CSV 的 409 也经显式确认后完成，没有静默复制。刷新后 12 份仍可见；从首页及单据列表返回批次，再追加一张图片，服务端为 13 份。旧单张入口实际上传另一张合成 PNG，创建单据 #1 且原件回读 1 份。最后经列表再次恢复并限速追加 2.4 MB 合成 PNG，进度显示捕获到 0%、1%、2% 等逐步上升值，批次回读为 **14 份**。另用 CDP 选择 `.txt` 与 20 MB+1 字节 PDF，本地分别提示格式与体积错误，服务端文件数仍为 14。`GET /api/intake-batches/current` 回读批次 #2、14 份；表格行 API 回读 2 行。以上是**合成文件的实际浏览器/API 验证**，不是客户原件或真实 OCR。
  - **命令与边界**：从仓库根目录运行 `npm.cmd --prefix frontend run typecheck`、`npm.cmd --prefix frontend run build:h5` 均退出码 0，`git diff --check` 退出码 0（只有现有换行转换提示）；后端聚焦测试如上。浏览器验证临时脚本在隔离目录运行，结束后清理，不作为持久测试产物。未运行真实 OCR、历史结果回放、App 真机或另一台物理设备；跨设备恢复由持久化批次、`/current` 与可分享的批次 URL 实现，本轮在同一台机器刷新与列表往返实测。M10—M12 的分类与审核浏览器闭环未提前验收。

### M10：前端卡片整理页与手机操作

- [x] 按本文件低保真图实现左待办、右日期/商家组；系统建议与人工确认显著区分。支持一张/多选拖拽、日期标题和具体商家组两级投放、高亮目标、叠放数量、撤销提示、原件预览。
- [x] 所有移动调用 M06 同一命令；卡片点击看详情，移动操作收在一个菜单，避免每张卡片堆满按钮。异步加载、空状态、处理失败、版本冲突均可恢复。
- [x] 约 390px 窄屏使用「点选内容 → 点目标组」完成所有归组，日期未列出时可选日期；测试触摸滚动、PDF 缩放与拖动互不抢手势。键盘与单点操作可完成拖拽等价行为。
- **验收**：三张商家未知内容一次进入 `11 月 8 日 / Costco`，另有一张只归日期后留在「商家待定」；撤销、刷新回读、窄屏点选均与服务端一致。
- **验证记录（2026-11-22，隔离环境 + Edge 无头浏览器实跑；真实扫描 PDF 原件 + 合成 PNG，Fake 识别）**：
  - **前置核对**：不只看勾选。M06 的 `/groups`、`/groups/items`、`/moves`、`/moves/<id>/undo` 四条 URL 与 `intake_moves` 迁移 `d06a4c71e812` 在代码与迁移里实际存在，本轮跑 `& .\backend\.venv\Scripts\python.exe -m pytest -c backend/pytest.ini -o addopts='' -q backend/tests/test_intake_moves.py backend/tests/test_intake_api.py` → **21 passed in 2.40s**。M09 的混合上传页、`/current` 恢复和 `?view=batch&batch=<id>` 在工作树里存在，`npm.cmd --prefix frontend run typecheck` 开工前即退出码 0。工作树里 M00—M09 的未提交改动全部保留，本步只做增量修改。
  - **修改文件**：新增 `frontend/src/features/invoices/IntakeOrganizer.vue`、`IntakeCard.vue`、`intakeBoard.ts`、`frontend/src/shared/platform/dragBoard.ts`；修改 `frontend/src/shared/api/index.ts`（五个读写函数）、`types.ts`（`IntakeItem` / `IntakeCandidate` / `IntakeRecognition` / `IntakeGroup` / `IntakeMoveResult`）、`workspaceNavigation.ts`（新增 `organize` 视图与 `?view=organize&batch=<id>`）、`IntakeBatchUpload.vue`（新增「按日期和商家整理」入口）、`pages/workspace/index.vue`（挂载整理页与往返）、`OriginalDocumentViewer.vue`（`invoiceId` 可为空、`eyebrow` 可覆盖，批次内容复用同一个查看器）、`frontend/README.md`、本节与 `backend/docs/impl_record.md`。**没有后端代码、接口、迁移或 OCR 规则改动**；完整后端套件 `& .\backend\.venv\Scripts\python.exe -m pytest -c backend/pytest.ini -o addopts='' -q backend/tests` → **1412 passed in 77.73s**。
  - **命令**：`npm.cmd --prefix frontend run typecheck` 与 `npm.cmd --prefix frontend run build:h5` 均退出码 0（金额写法那处修复之后重跑并重新发布构建，真实 OCR 的浏览器闭环用的就是这份构建）；`git diff --check` 退出码 0（只有既有的换行转换提示）。
  - **桌面 1280px 实跑**（隔离 SQLite + `OCR_PROVIDER=fake` 的 Flask、Node 静态服务器发布构建、Edge 无头 CDP 真实鼠标与键盘事件）。素材：**真实扫描件 `samples/real/04_NewMoys_Meat_5p.pdf` 切出 5 页** + 2 张合成 PNG，共 7 张卡片，`/recognize` 用 Fake 识别器各给一条 `paper` 商家候选。逐条结果：
    - 卡片上三类信息分开：虚线「建议 … · 纸面读出」、橙色「缺日期和商家」、实心「已确认」为 0 条、另有「演示识别结果」，识别说明为「纸面日期读不出来：认不出日期写法 'FAKE 2026-11-09'」——不是一律「未知」。
    - **验收①**：点选 3 张 → 「移到…」填 `2026-11-08` + 新商家 `Costco` → 服务端 `2026-11-08|Costco|3`、待归类 `4`。
    - **验收②**：真实鼠标按住手柄拖到日期标题，拖动中预览显示「1 项」、目标区 `.over` 高亮、3 个投放区亮起；松手后 `2026-11-08|none|1`（**只写日期，商家仍待定**）。
    - 撤销：点「撤销这一步」后回到 `2026-11-08|Costco|3` + 待归类 `4`。
    - 多选拖拽：选 2 张拖到 Costco 组，拖动中叠放预览显示「2 项」，结果 `2026-11-08|Costco|5`。
    - 刷新回读：重新打开 `?view=organize&batch=1`，Costco 显示 5 项、待归类 2 张，与服务端一致。
    - 组内回读与原件预览：展开 Costco 的 5 张卡片都带「已确认 · 11 月 8 日（2026） · Costco」；点卡片正文打开**真实扫描 PDF**，pdf.js 实际渲染出 944×1230 的页面、标题为「批次原件 · 第 1 页 · PDF 共 5 页」，点「下一页」到 `2 / 5 页`。
    - 键盘等价：焦点落在组的「移入所选」按钮上按 **Enter**，移动成功（`2026-11-08|Costco|6`）。
    - **双会话冲突**：页面读完卡片后，另一个会话用 API 把同一张移到 `2026-11-09 / 华丰商行`；页面再点「移入所选」得 409，界面显示「内容版本已改变，请刷新后重试。（没有内容被改动）」，服务端仍是另一会话的结果，页面随即重读并显示出 `华丰商行` 这一组——**人工决定没有被覆盖**。
  - **窄屏 390×844 实跑**（同一环境，`mobile: true` + 触摸模拟，全部用 `Input.dispatchTouchEvent` 真实触摸事件）：
    - 布局为「待归类 / 日期分组」双 pane，横向溢出 **0px**，选择控件 44px。
    - 触摸点选 2 张 → 切 pane 后仍显示「已选 2 项」（选择跨 pane 保留）。
    - 空板上用「移到…」填日期 + 新商家建出第一个组（`2026-11-08|Costco|2`）；再点选 1 张、切到分组 pane、点「移入所选」→ `2026-11-08|Costco|3`，**全程没有用拖拽**。
    - 日期未列出：面板里填 `2026-11-12` → `2026-11-12|none|1`。
    - **触摸滚动不被拖拽抢走**：手指按在卡片正文上滑 260px，页面从 0 滚到约 245px，没有出现拖动预览，分组未变。
    - **拖到原处不浪费一次移动**：触摸拖手柄到自己所在的日期标题，预览与高亮都出现，但提示「选中的内容已经在 11 月 8 日（2026） / 商家待定，这一步没有改动任何内容。」且分组未变。
    - **触摸拖拽确实能完成移动**：手指按住 `11 月 12 日 / 商家待定` 那张卡片（真实扫描件第 4 页）的手柄，拖到 `11 月 8 日` 标题：拖动中预览显示「1 项」、目标高亮，松手后服务端 `2026-11-12` 这一组消失、出现 `2026-11-08|none|1`——**只改了日期，商家仍是待定**。
    - **PDF 缩放/滚动与拖拽不冲突**：窄屏打开真实扫描 PDF，点「放大」后工具栏从「适应宽度」变 `1.5×`，在预览里触摸滑动，没有出现拖动预览，分组未变。
  - **未测 / 未覆盖**：①**真实 OCR 与历史结果回放都没跑**——本轮识别全部来自 Fake 识别器，卡片上的商家建议是演示数据，界面也照规矩标了「演示识别结果」，不能据此谈识别准确率；②**App 真机没测**，拖拽适配器在非 H5 平台直接返回「不支持」并退回按钮路径，但这条没有在真机上验证；③**窄屏触摸拖拽只在源和目标同屏时可用**——没有做拖拽时的自动滚屏，所以手机上的主路径是点选 + 点目标组（第 4 节允许拖拽只作快捷方式）；④**已送审内容的拦截只靠服务端**：`/items` 的响应里没有 `handed_off_at`，所以卡片在送审后看起来仍可移动，真正拦住它的是服务端 409（文案为「内容已进入审核，请通过审核修改流程更正。」）。M12 把闭环接起来时要么在卡片上标出这个状态，要么在契约里补这个字段；本步没有改后端契约，也没有制造出已送审的内容来实测这条 409；⑤**M07 的相似堆与商家记忆没有接进界面**（本步按计划只用固定候选，不依赖 M07）；⑥真实原件只用了一份 5 页扫描 PDF，HEIC、CSV/XLSX 行的卡片在整理页上只走过代码路径（行卡片的原件预览按现有查看器提示下载），没有在浏览器里逐项核对；⑦**没有测量 M00 定义的错归率、人工操作次数与整理耗时**——那是 M12 的验收项，本轮只验证操作与服务端一致。
  - **环境**：隔离库与上传目录建在会话临时目录（每轮新建、端口独占），验证结束后已停掉本轮起的 34 个本机服务并删掉这些库、上传目录和浏览器配置；没有碰仓库里的 `var/`，也没有动别人占用的 5000/5173 端口。遗留一个：最早一轮起在 `127.0.0.1:5055` 的 Flask 进程在本 shell 里看不到 PID，`taskkill` 报「找不到进程」，它仍占着临时目录下的一个 SQLite 文件；只影响会话临时目录，不影响仓库。⚠️ 过程中发现**Windows 允许第二个进程绑定已在监听的端口**，请求会落到先应答的那一个——一轮验证因此把数据写进了另一个并行服务的库，脚本随后改为先检查端口再启动。

### M11：前端财务表格视图

- [x] 在同一批次提供按日期、商家、待定原因筛选的连续表格；支持多选批量改日期/来源、快速看原件和进入明细审核，必要时用键盘加速，不做第二套分类状态。
- [x] 大表分页/虚拟显示，保留原 CSV 行号或 PDF 页码证据；候选值和人工确定值可区分，数字仍遵守现有字符串精度规则。
- [x] 切回 M10 卡片页后位置、选中的批次和已保存的移动结果一致；手机仍以卡片页为主，不把桌面表格硬缩到窄屏。
- **验收**：在表格中批量移动五行，卡片页立即显示同一结果；表格中可找出来源缺失的行并回看原件；大表不一次渲染全部行。
- **验证记录（2026-11-22；隔离库、真实 PDF 本地摄入与 Edge 无头浏览器）**：
  - **前置与工作树**：开工 `git status --short` 见 M00—M10 大量未提交成果，均保留；实际核对 M06 的 `/groups`、`/moves`、撤销实现与 `test_intake_moves.py`，M09 的批次恢复组件、`/current` 和 URL。开工前已有 M10 的卡片页及浏览器验证记录。默认命令执行器在启动前报 `helper_unknown_error: setup refresh had errors`；按本计划约定提升权限重试同一只读 `Get-Location` 成功，之后必要命令沿用已验证方式；没有推断中文路径是错误原因。
  - **修改与取舍**：新增 `frontend/src/features/invoices/IntakeLedger.vue`；修改整理页、workspace 路由和导航、API 类型与调用、`frontend/README.md`。表格按 `/items` 一次 50 条读取，筛选在后端 Repository 层的 `LIMIT/OFFSET` 前执行；日期、商家、待定原因和识别失败组合筛选共用原内容表，不另存分类状态。`/items` 新增可选 `purchase_date`、`source_name`、`missing`，内容响应新增 `row_values`，保留表格解析出的数字字符串；为此修改 `backend/src/api/intake.py`、`schemas.py`、`services/intake.py`、`repositories/intake.py`、`backend/README.md` 和两处现有测试。**没有迁移、OCR 提示词或模型调用规则改动**。多选日期/来源修改继续使用 M06 的 `/moves`（含版本、幂等键与撤销），单项进入明细审核使用 M08 `/reviews`；窄屏只提供回卡片页入口。浏览器实跑发现 uni-app 把 `<input type="checkbox">` 当普通文字输入渲染，已改用 `<checkbox>`。
- **自动化**：根目录运行 `npm.cmd --prefix frontend run typecheck`、`npm.cmd --prefix frontend run build:h5`，均退出码 0；`& .\backend\.venv\Scripts\python.exe -m pytest -c backend/pytest.ini -o addopts='' -q backend/tests/test_intake_moves.py backend/tests/test_intake_spreadsheets.py` → **12 passed**（新增数据库先筛选再分页、日期/商家缺失口径、CSV 原始行号与数字字符串断言）；完整 `... -q backend/tests` → **1413 passed in 56.58s**；`git diff --check` 退出码 0（只有现有 LF/CRLF 提示）。新测试首次收集时有一处断言少右方括号，修正后上述回归均通过。
  - **浏览器实跑**：隔离 SQLite `alembic upgrade head` + Fake OCR 配置、本地服务器、Edge 无头 1280px 视口；上传仓库现有真实扫描 PDF `04_NewMoys_Meat_5p.pdf`、`01_ChangShun_Market_32p.pdf`、`02_FengYuan_Meat_18p.pdf`，本地切出 **55 页**，未运行 OCR。财务表格第 1 页实际渲染 **50 行**、第 2 页 **5 行**；从第一张打开原件，服务端原始字节接口 200、PDF 查看器显示 **共 5 页**。勾选 5 行，用人为填写的 `2026-11-08 / Costco` 批量归类，服务端筛选回读恰好 5 行；「商家缺失（含日期缺失）」筛出剩余 **50 行**。切到卡片页，已归类 5 项；再切回表格，筛选与页位置保留；按日期/商家筛出 5 行，点其中一行进入现有明细审核，创建并打开草稿 #1。这里的 Costco 和日期是**测试输入，不是真实 PDF 的真值**。临时静态服务器初次未给 pdf.js `.mjs` worker 正确 MIME，修正验证脚本后才成功预览；这不是产品代码缺陷。
  - **边界**：本步没有真实 OCR、历史响应回放、App 真机或真实 CSV/XLSX 的浏览器操作；CSV 行号与字符串值由合成 CSV 的后端测试核对。55 行只证明分页而非大规模性能上限；真正的混合上传到价格历史闭环、真实来源/日期准确率与人工操作耗时留在 M12。无额外样本前置要求。

### M12：前后端闭环与现有样本验收

- [x] 从干净迁移库跑后端定向测试、`python -m pytest`、前端 typecheck/build；用隔离环境从浏览器实际完成「混合上传 → 整理 → 填写 → 确认 → 价格历史回查」。Fake 与真实 OCR 的验证分开记录。
- [x] 用仓库现有真实原件验证其实际涵盖的印刷、手写、小票与数字 PDF 路径；用明确标为合成的文件验证五页跨日期、同日同商家多单、同文件跨商家、双语/仅中文/无商家、跨日期来源 CSV、无日期、疑似重复和中断恢复等缺失边界。桌面与窄屏操作用实际浏览器验证，不能把合成文件说成客户原件。
- [x] 在可核对样本与实际浏览器批次上报告 M00 定义的错归日期、错归商家、漏项、人工操作次数和整理耗时；没有真值的指标标未测。逐张填写来源的对照若没有可比操作记录，列为未测而不虚构耗时。来源识别与视觉相似建议的效果分开报。
- [x] 核对旧单张流程、旧导入、原件回看、对账、撤销确认及入账修订；更新 `backend/README.md`、`frontend/README.md` 与实施记录，明确 App 真机若未测则不得称已验收。
- **验收**：上述功能链路在实际浏览器与 API 上可复现；旧数据迁移不丢；报告分别列出真实原件、合成边界、历史回放和未测范围。额外客户原件不是完成条件。
- **验证记录（2026-11-22 — 2026-11-23；隔离库 + Edge 无头浏览器实跑；先用 Fake 识别器跑完功能链路，再按用户授权用 `OCR_PROVIDER=dashscope` 真实识别器全量实跑一遍）**：
  - **前置核对（不只看勾选）**：M01—M11 的实现在工作树里实存——32 条 `/api/intake-batches` 与 `/api/source-memory` 路由（实际从 `app.url_map` 数出来的）、六个 `intake_*` 迁移（head 为 `f2a9c35db714`）、前端 `IntakeBatchUpload.vue` / `IntakeOrganizer.vue` / `IntakeCard.vue` / `IntakeLedger.vue` / `intakeBoard.ts` / `dragBoard.ts`。开工前的完整后端套件 **1413 passed in 56.16s**，`npm.cmd --prefix frontend run typecheck` 退出码 0。工作树里 M00—M11 的未提交改动全部保留，本步只做增量修改。默认 Bash 执行器本会话一次就起来了，没有出现 `helper_unknown_error`，因此没有做提升权限的空探测。
  - **代码改动（只补闭环缺的那一块，没有新业务规则）**：
    - 后端：`src/services/intake.py`（`IntakeItemView` 加 `handed_off_at`）、`src/api/schemas.py`（`intake_item_payload` 透出它）。**没有迁移**——这一列 M08 已经在 `intake_items` 上了，M12 只是把它透出到接口。
    - 前端：`shared/api/types.ts`（`IntakeItem.handed_off_at`）、`shared/api/index.ts`（新增 `handOffIntakeItems`，`IntakeLedger` 原来内联的 `apiRequest` 改用它）、`features/invoices/intakeBoard.ts`（`isHandedOff`、`handoffProblem`）、`IntakeCard.vue`（「已送审」标记 + 一句怎么改 + 不再给拖动手柄）、`IntakeOrganizer.vue`（组上的「送入审核」、送审后本地拦住移动、`review` 事件）、`IntakeLedger.vue`（已送审行标出来、按钮变「打开审核」、批量改分类跳过这些行）、`pages/workspace/index.vue`（接 `@review`）。
    - 测试与工具：新增 `backend/tests/test_intake_closed_loop.py`（3 项）与 `backend/tools/eval_multifiles_m12.py`（真实原件的路径覆盖，离线、不调模型）。
    - **真实 OCR 跑出来才暴露的一处修复**：`frontend/src/features/invoices/lineDraft.ts` 新增 `plainAmount()`，`LineEditor.vue` 的金额字段与 `InvoiceHeaderEditor.vue` 的总额在「此数据正确」时调用它。原因见下面的真实 OCR 记录——模型给的是纸面原文 `$1,040.00`，而金额框只收 `1040.00`，照单收下会把整张单卡死。
    - 工具：新增 `backend/tools/eval_multifiles_m12_live.py`（把真实识别的报告按 M00 口径对着仓库自己的逐字段真值打分，离线、不调模型）。
    - **为什么要加卡片页的「送入审核」**：M11 之前只有财务表格能逐行送审，而财务表格**窄屏不提供**，所以手机上走不到「填写 → 确认」。闭环是本步的交付物，这一条按钮是它缺的最后一段。送的是**选中的那几项**而不是整组：一个日期/商家组可能装着几张真正的发票。
  - **自动化**：从仓库根目录 `& .\backend\.venv\Scripts\python.exe -m pytest -c backend/pytest.ini -o addopts='' -q backend/tests` → **1416 passed**（1413 + 本步 3 项；跑了三轮，60.31s / 72.05s，真实 OCR 那处前端修复之后又跑了一轮 76.70s）；`npm.cmd --prefix frontend run typecheck` 与 `npm.cmd --prefix frontend run build:h5` 均退出码 0；`git diff --check` 退出码 0（只有既有的 LF/CRLF 提示）。干净迁移库：`DATABASE_URL=sqlite:///<临时路径> alembic upgrade head` 从零升到 `f2a9c35db714` 成功，`alembic current` 确认 head；浏览器验证全程用的就是这个从零建起来的库。
  - **桌面 1280px 浏览器实跑（合成文件，Fake 识别，failures=0 / 72 项断言）**：隔离 SQLite + 隔离上传目录 + `OCR_PROVIDER=fake` 的 Flask（127.0.0.1:5311）、发布构建经本地静态服务器同源代理（5312）、Edge 无头 CDP 真实鼠标与键盘。逐条结果：
    - 混合上传 8 份（五页 PDF、同日两单 4 页 PDF、同文件跨商家 3 页 PDF、无商家 1 页 PDF、双语 PNG、仅中文 PNG、内容损坏的 PNG、跨日期来源 CSV）：服务端回读 7 份 `ready`、坏图 1 份 `failed`（`content_unsupported`），五页 PDF `item_count=5`，CSV 5 行，共 **20 项可归类内容**。
    - CSV **停在「批次表格映射」**没有按自动匹配直接入批次；点「预览表格」实际渲染「表格预览 · CSV · 5 行」，勾选「我已核对表格列…」后点「加入整理批次」才进去。
    - 疑似重复：同字节换名重传，批次仍是 8 份，界面写「这份文件已保存在当前批次。」
    - 中断恢复：回首页点「接着整理」→ `?view=batch&batch=1`、标题「混合文件整理批次 #1」、「已保存的文件 · 8」。
    - 整理：点选同日两单那份 PDF 的 4 页 → 「移到…」填 `2026-11-08` + 新商家 `SynthMart Wholesale` → 服务端 `2026-11-08|SynthMart Wholesale|4`，待归类 16。
    - **同组两张发票各自成为一张草稿**：选第 1、2 页点「送入审核」→ 单据 #1；选第 3、4 页 → 单据 #2。两张草稿的 `invoice_date` / `source_name` 都是整理页上人工填的 `2026-11-08 / SynthMart Wholesale`，`intake_sources` 各带 2 页回指。**没有合并成一张合计发票**。
    - 送审后 4 张卡片全部标「已送审」+「要改日期或商家，请在审核里修改分类」，拖动手柄 0 个；`/items` 的 `handed_off_at` 4 项非空。再在界面上试着把其中一张移到 `2026-11-12`：页面先说「这一项已经进入单据审核…」，服务端分类仍是 `2026-11-08`。
    - 填写 → 确认 → 收口：两张草稿的页**文字层没有可解析的明细行，草稿是 0 行**（这是诚实而不是失败），按界面的「手工加一行」各补 1 行，逐行确认各 1 行，`/complete` 各回 `completed`。
    - **价格历史回查**：价格页「采购历史」页签实际显示两条「2026-11-08 · SynthMart Wholesale」；`/api/prices/food-items/1/history` 回 2 条，`purchase_date` / `source_name` 与整理页一致、`source_invoice` 非空（能回到单据）。
    - 财务表格：同一批次 20 行，其中 4 行状态「已送审」、按钮「打开审核」。
    - 旧路径回归：旧单张上传入口直接进审核（单据 #3，`intake_sources=0`、自己带 1 份文件）；旧 `/api/imports` 按整份表建单据 #4、3 行。
    - 原件回看：整理页点卡片正文，pdf.js 实际渲染 929×1202，弹层标题带「第 1 页」与两条「建议 … · 纸面读出（未确认）」。
  - **窄屏 390×844 实跑（全部用 `Input.dispatchTouchEvent` 真实触摸，failures=0）**：横向溢出 **0px**；双 pane「待归类 / 日期分组」；**窄屏不提供财务表格入口**。触摸点选 4 项 → 「移到…」建出 `2026-11-10 / SynthMart Wholesale`；切到分组 pane、点选 1 项、点「送入审核」→ `?view=review&invoice=5`，**全程没有用拖拽**。回整理页那张卡片标「已送审」、无手柄；再试着移动被界面拦住，服务端分类不变。
  - **仓库现有真实原件（`backend/tools/eval_multifiles_m12.py`，离线、0 次模型调用）**：10 份原件摄入 **83 个内容单元**，与 M00 清单的 83 项一致。按本地笔迹测量分层：**印刷 6 页**（`03_Kunkee_Trading_1p.pdf` 1 页 + 四份手写扫描里实测为 `printed` 的 5 页）、**手写（`non_uniform`）68 页**、**零售小票 4 张**、**数字 PDF 5 页**——与 M00 的 6/68/4/5 完全对上。⚠️ **发现一处仓库事实**：`samples/real/1.png`—`4.jpg` 与 `samples/real/receipts/06..09` 是**同一批字节**（sha256 相同），不是八张不同的小票；再传一遍会撞上「同一份文件不用传两次」的 409，这反过来证明了判重是对的。数字 PDF 走**真实文字层**：5 页、**模型调用 0 次**，纸面日期（人工转录）**5/5 出现在候选里**、5/5 标为日/月歧义留两种读法；**商家候选 0/5**——这两条是既定行为而不是识别退化（没人确认商家时 `date_order` 是 `strict`；这份 PDF 的卖方是页首一行没有标签的标题，唯一带标签的一方是 `Bill to:` 的买方）。另在浏览器里把这 10 份真实原件传进一个批次（83 页），从整理页打开第一张，pdf.js 实际渲染 929×1210。
  - **M00 口径的指标，按实际能算的报**：
    - **错归日期 / 错归商家**：本轮**系统没有把任何内容自动放进组**（分组只由人工决定派生），所以 M00 的「自动放组且可核对」分母是 **0**，错误归组率**无法计算**，不是 0%。
    - **未归组率**：桌面那一批 20 项里 4 项归了组、16 项留在待归类——这是我**只整理了演示需要的那 4 页**的结果，不是系统漏项，不能当质量指标读。
    - **人工操作次数**：桌面 1 次批量归组（影响 4 项）+ 2 次送审（各 2 项）＝ 3 条发给服务端的命令，另有 1 次被界面本地拦住的移动（没有发请求）；窄屏 1 次归组（4 项）+ 1 次送审（1 项）+ 1 次被拦住。
    - **整理耗时、与「逐张填写来源」的耗时对照**：**未测**。本轮是脚本驱动的浏览器操作，脚本的墙上时间不是人的操作时间，没有可比的人工操作记录。
    - **来源识别效果**：已用真实识别器在全部 83 个单元上实测，分层结果见下面那一条（印刷 6/6 正确；小票 3 张「纸面没有就不编」正确、1 张漏读；手写 61/68 有候选且不跨叠串商家，但仓库没有逐页真值，只能报覆盖与一致性）。**视觉相似建议（M07 的相似堆）仍没有接进界面**，所以它的效果本轮**没有单独报**。
  - **真实 OCR 全量实跑（2026-11-23；用户授权花钱。`OCR_PROVIDER=dashscope`，crosscheck 双模型 `qwen3.5-ocr` + `qwen3-vl-plus`，提示词 SHA `67fb7df1217b`）**：
    - 命令：`tools/eval_multifiles_m05.py live --include 03_Kunkee_Trading_1p.pdf`（先花 1 页的钱验通凭据）→ `tools/eval_multifiles_m05.py live --keep-db`（其余 82 项）→ `tools/eval_multifiles_m12_live.py --report <smoke> --report <full>`（离线打分）。报告落在被忽略的 `samples/real/m05/`（含客户原文，不进仓库）。
    - 规模：**83 个内容单元全部跑完、0 失败**，**156 次模型调用**、合计 **690.89 秒**（约 11.5 分钟）。数字 PDF 的 5 页走文字层、**0 次调用**；第二轮里那 1 页已经识别过的内容**一次都没有再收费**——「不重复收费」这条第一次是在**付费的真实数据**上验的。
    - **印刷 6 页（有逐字段人工真值）**：商家 **6/6 正确**、日期原文 **6/6 与纸面一致**、明细行数 **6/6 一致**。
    - **零售小票 4 张（有逐字段人工真值）**：3 张纸面没有可读来源 → **一个都没编造**（正确）；1 张纸面印着 `LOWE'S HOME CENTERS` 却**没给候选**（漏读）。日期：1 张原文一致、1 张纸面本来就没有日期、**2 张带时间的收银小票没读出日期原文**（`21.12.26 20:37:43`、`2017.11.22 09:14`）。行数：3 张一致，**IKEA 那张多报**（13 行 vs 纸面 7 行）。
    - **中文手写 68 页（仓库没有逐页人工真值）**：0 失败；**61/68 给出商家候选，且都与该叠原件文件名所指的商家一致**，7 页没有候选；36/68 读到日期原文。⚠️ 这是**覆盖与一致性**，**不是准确率**——文件名只能证明「没有跨叠串商家」，证明不了每一页读对了。
    - **数字 PDF 5 页**：0 次调用、5/5 读到日期原文、商家候选 0/5（既定行为，理由同上一条）。
    - **分母**：准确率只来自那 **10 个有逐字段人工真值的单元**（印刷 6 + 小票 4）；其余 73 个单元只报覆盖与一致性，不进任何准确率的分母。
  - **真实识别器下的浏览器闭环（隔离库 + 真实识别器 + Edge 无头，`failures=0`）**：素材是 3 份**真实原件**——印刷扫描 1 页、中文手写 1 页、数字 PDF 5 页，共 7 个单元。逐条结果：`/recognize` **4 次调用**（两张扫描页各 2 个模型）、数字 PDF 0 次 → **再驱动一次 0 次调用** → 整理页**没有**「演示识别结果」标记（识别器是真的）→ 卡片上直接读出 `KUN KEE TRADING INC.` 和 6 月 26 日，手写页读出 `FENG YUAN MEAT MARKET INC.` 并把 `6/1/26` 的两种读法都留着 → 候选**没有**写进 `decided_*`（7 项全待人工确认）→ 人照着建议归组 → 卡片页「送入审核」→ 草稿**正好 1 行**（与纸面相同），值以「AI 识别 · 待核对」摆在输入框里而不是当成已填 → 在浏览器里逐字段按「此数据正确」（商品名称、数量、单价；单号 180311、总额 1040.00）→ 人补上模型**没给**的采购单位与行金额（两个模型对行金额有分歧，分歧字段不预填）→ 选分类 → 「确认此行并入账」（食材随第一次采购建出来）→ 勾「我已检查原件有无漏行」→ 「完成单据」得 `completed`，**对账差额 0.00** → 价格历史页查到 `2026-06-26 · KUN KEE TRADING INC.`，接口里单价 `52.00`、`source_invoice` 指回这张单。
  - **真实 OCR 才暴露出来的一处缺陷，已修**：模型给的是**纸面原文** `$1,040.00`，而金额输入框只收 `1040.00`（校验里写明不收货币符号和千分位）。于是点「此数据正确」接受总额 → 单据头存不进去 → 之后**每一行**的「确认此行并入账」都被挡在「请先检查并保存单据信息」上，而真正的原因（金额写法）只躺在单据头那个字段里，人看不出来。修法：接受识别值时按输入框的形状收下来（`plainAmount()`，去货币符号、空格与千分位逗号），**去不干净的写法原样留着**，让字段原有的报错照常出现——宁可让人自己决定，也不替人猜一个金额。⚠️ Fake 识别器给的一直是规范数字，所以这条路径**在 Fake 下永远测不出来**；它是这次花钱跑真实识别换来的。
  - **未测 / 未覆盖（都是真的没做，不是忘了写）**：
    1. **手写 68 页仍然没有「准确率」**——真实 OCR 已经把它们全跑完了（0 失败、61/68 有商家候选），但仓库里**没有逐页人工真值**，所以只能报覆盖与一致性。要给手写层一个准确率，需要先逐页人工标注，那是另一件事，不是跑一次模型能换来的。
    2. **历史结果回放没有在本步重跑**（M00 的 `baseline.json` 仍是那份基线）。
    3. **App 真机没测**——混合文件选择、拖拽适配器在非 H5 平台的退化路径、系统返回键都只走过代码路径。**不得称 App 已验收。**
    4. **跨设备恢复**只在同一台机器上用刷新、列表往返和可分享的批次 URL 验证，没有第二台物理设备。
    5. **双语 / 仅中文 / 无商家**三种版式：双语与仅中文是**合成 PNG 占位图**（PDF 文字层画不出 CJK），所以它们只证明了摄入与卡片路径，**没有**证明识别在这三种版式上的表现；无商家那页是合成 PDF。M01 的契约测试仍是这三条规则的证据来源。
    6. **五页跨五天、同文件跨商家**两条边界的**浏览器**操作没有逐条走完（后端测试与 M08 已覆盖）；本轮浏览器里实际走完的是「同日同商家两张发票各自收口」。
    7. **CSV 的无日期行在预览里没有报「需要检查」**——空单元格是「没填」而不是「填错」，所以它不产生 `review_issues`，那一行照常进批次、留在待归类。这是观察到的既有行为，本步没有改。
    8. **大规模性能**没测：最大一批是 83 页。
  - **环境收尾**：隔离库、上传目录、浏览器配置都建在会话临时目录，验证结束后停掉本轮起的 Flask 与静态服务器并删掉这些目录；没有碰仓库里的 `var/`，也没有占用 5000/5173。沿用 M10 的教训：每次起服务前先查端口，避免 Windows 允许第二个进程绑定同一端口而把数据写进别人的库。

## 7. 默认口径与需要在样本验证的决定

为避免 M00—M06 因缺口径无法开工，先按以下默认值实施；真实样本或用户后续决定可修正，修正时记录影响：

1. 「商家」先按用户确认的来源名归组；大小写和空白作规范化，不自动把两个 Costco 门店合并为同一来源。是否按品牌/公司还是门店统一归组，用 M00 样本和实际使用方式再确认。
2. 分类日期是**实际采购日**；纸面日期保留原文。二者不同时，用户可更正分类日期。对只有纸面日期的单据先作为候选，入账前供用户核对。
3. 系统建议可先展示在日期/商家组里，但必须显著标明尚未人工确认；不会仅凭候选自动入账，也不会以商家候选去覆盖纸面明确相反的证据。

