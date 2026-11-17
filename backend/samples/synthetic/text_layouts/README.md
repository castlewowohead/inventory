# 英文合成文本样本（步骤 07.10）

改写自 `kitchen-ledger/tests/fixtures/`，由 `tools/gt_from_legacy_fixtures.py` 生成。
**运行时与测试时都不读旧目录**，这里的文件是本项目自己的副本。

| 文件 | 内容 |
| --- | --- |
| `<版式>.txt` | 发票文本原文 |
| `<版式>.json` | 对应的 Ground Truth，字段名对齐 `src/contracts.py` |
| `freshfields_ocr_output.txt` | 旧项目一份 OCR 输出原文，留作解析回归输入 |

覆盖的边界：列序颠倒、无 SKU 与货币符号、描述换行、小数点用逗号、多档税、
按重量计价。

## 这批样本不能用来做什么

- **不是中文手写 OCR 的性能证据**：英文、合成、而且是文本不是图像。
- **不把旧的 Tesseract 英文方案设为默认**：步骤 07 的选型结论只针对阿里云
  Model Studio 的可用型号，与这批样本无关。
- `baseQuantity` 等换算后的派生值已丢弃，换算属步骤 09。
