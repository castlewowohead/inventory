"""把扫描仪产出的 PDF 拆成单页图片（步骤 07 的评测前置工具）。

**为什么需要它**：`samples/real/` 里的五个 PDF 全部由 `Scanner System` 产出，每页
是一整张 JPEG，没有文字层（`/Font` 为 0，解压后没有任何文字算子）。OCR 厂商的接口
基本只接受图片，所以评测前必须先拆页。同时这批 PDF 的「一页」就是「一张独立单据」
（同一个文件里的相邻两页单号与日期都不同），拆页之后才能按单据逐张标注与评分。

**这个工具只处理一种 PDF**：每页恰好内嵌一张 DCTDecode（JPEG）全页图。遇到别的结构
（多图拼版、文字层 PDF、CCITT/JPX 编码、对象流压缩）**直接报错退出，不猜、不降级**。
原因是拆错页会让后面所有的 Ground Truth 与评分跟着错位，而且不会报错——这种错误最难
发现。真正通用的 PDF 处理属于步骤 08，到时按实测需要引入正式依赖。

**不引入第三方依赖**：只用标准库。它是评测辅助工具，不属于运行时代码路径，因此
不进 `requirements.txt`，也不会被 `src/` 导入。

用法：
    python tools/split_scanned_pdf.py samples/real --out samples/real/pages

输出文件名形如 `01_ChangShun_Market_32p__p07.jpg`，页码从 1 开始、补零对齐，
与人翻单据时说的「第几页」一致（与 `src/contracts.py` 的 `FieldEvidence.page` 同口径）。
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# PDF 对象头：`12 0 obj`。这批文件是 PDF 1.4、无对象流（/Type /ObjStm 计数为 0），
# 因此可以直接在字节流里定位对象，不需要完整的 PDF 解析器。
_OBJ_HEADER = re.compile(rb"(?<![0-9])(\d+)\s+(\d+)\s+obj\b")
_PAGES_OBJ = re.compile(rb"/Type\s*/Pages\b")
_KIDS = re.compile(rb"/Kids\s*\[(.*?)\]", re.S)
_REF = re.compile(rb"(\d+)\s+(\d+)\s+R")
_XOBJECT = re.compile(rb"/XObject\s*<<(.*?)>>", re.S)
_STREAM_START = re.compile(rb"stream\r?\n")


class NotASimpleScanError(RuntimeError):
    """PDF 不是「每页一张整页 JPEG」的结构。调用方应当停下，而不是改用猜测。"""


def _object_table(data: bytes) -> dict[int, int]:
    """对象号 -> 该对象在文件中的起始偏移。重复定义取最后一次（增量更新）。"""
    table: dict[int, int] = {}
    for match in _OBJ_HEADER.finditer(data):
        table[int(match.group(1))] = match.end()
    return table


def _object_body(data: bytes, table: dict[int, int], number: int) -> bytes:
    start = table.get(number)
    if start is None:
        raise NotASimpleScanError(f"找不到对象 {number}")
    end = data.find(b"endobj", start)
    return data[start : end if end != -1 else len(data)]


def _page_numbers_in_order(data: bytes, table: dict[int, int]) -> list[int]:
    """按页面树 /Kids 的顺序返回页对象号，而不是按它们在文件里出现的先后。"""
    pages_match = _PAGES_OBJ.search(data)
    if pages_match is None:
        raise NotASimpleScanError("没有找到页面树（/Type /Pages）")
    kids_match = _KIDS.search(data, pages_match.end() - 200)
    if kids_match is None:
        raise NotASimpleScanError("页面树里没有 /Kids 数组")
    kids = [int(ref.group(1)) for ref in _REF.finditer(kids_match.group(1))]
    if not kids:
        raise NotASimpleScanError("/Kids 数组是空的")
    return kids


def _image_ref_of_page(data: bytes, table: dict[int, int], page_obj: int) -> int:
    body = _object_body(data, table, page_obj)
    xobject = _XOBJECT.search(body)
    if xobject is None:
        raise NotASimpleScanError(f"页对象 {page_obj} 没有 /XObject，可能不是整页扫描图")
    refs = _REF.findall(xobject.group(1))
    if len(refs) != 1:
        raise NotASimpleScanError(
            f"页对象 {page_obj} 引用了 {len(refs)} 张图；本工具只处理每页一张整页图"
        )
    return int(refs[0][0])


def _jpeg_bytes(data: bytes, table: dict[int, int], image_obj: int) -> bytes:
    start = table.get(image_obj)
    if start is None:
        raise NotASimpleScanError(f"找不到图像对象 {image_obj}")
    stream = _STREAM_START.search(data, start)
    if stream is None:
        raise NotASimpleScanError(f"图像对象 {image_obj} 没有 stream")
    header = data[start : stream.start()]
    if b"/Subtype" not in header or b"/Image" not in header:
        raise NotASimpleScanError(f"对象 {image_obj} 不是图像")
    if b"/DCTDecode" not in header:
        raise NotASimpleScanError(
            f"图像对象 {image_obj} 不是 JPEG（/DCTDecode）；本工具不做格式转换"
        )
    end = data.find(b"endstream", stream.end())
    if end == -1:
        raise NotASimpleScanError(f"图像对象 {image_obj} 的 stream 没有结束标记")
    blob = data[stream.end() : end].rstrip(b"\r\n")
    if not blob.startswith(b"\xff\xd8"):
        raise NotASimpleScanError(f"图像对象 {image_obj} 的内容不是 JPEG 数据")
    return blob


def split(pdf_path: Path, out_dir: Path) -> list[Path]:
    """拆一个 PDF，返回写出的图片路径。结构不符合预期时抛 NotASimpleScanError。"""
    data = pdf_path.read_bytes()
    table = _object_table(data)
    pages = _page_numbers_in_order(data, table)

    declared = len(re.findall(rb"/Type\s*/Page[^sC]", data))
    if declared != len(pages):
        raise NotASimpleScanError(
            f"页数对不上：/Kids 给出 {len(pages)} 页，文件里有 {declared} 个页对象"
        )

    out_dir.mkdir(parents=True, exist_ok=True)
    width = max(2, len(str(len(pages))))
    written: list[Path] = []
    for index, page_obj in enumerate(pages, start=1):
        image_obj = _image_ref_of_page(data, table, page_obj)
        blob = _jpeg_bytes(data, table, image_obj)
        target = out_dir / f"{pdf_path.stem}__p{index:0{width}d}.jpg"
        target.write_bytes(blob)
        written.append(target)
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__ and __doc__.splitlines()[0])
    parser.add_argument("source", type=Path, help="PDF 文件或包含 PDF 的目录")
    parser.add_argument("--out", type=Path, required=True, help="图片输出目录")
    args = parser.parse_args(argv)

    source: Path = args.source
    pdfs = sorted(source.glob("*.pdf")) if source.is_dir() else [source]
    if not pdfs:
        print(f"{source} 下没有 PDF", file=sys.stderr)
        return 2

    total = 0
    failed = 0
    for pdf in pdfs:
        try:
            written = split(pdf, args.out)
        except NotASimpleScanError as error:
            failed += 1
            print(f"[跳过] {pdf.name}：{error}", file=sys.stderr)
            continue
        total += len(written)
        print(f"[完成] {pdf.name} -> {len(written)} 页")
    print(f"\n共写出 {total} 张图到 {args.out}" + (f"；{failed} 个文件被跳过" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
