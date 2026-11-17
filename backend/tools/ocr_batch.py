"""批量调用 OCR 服务识别单页图，逐页落盘原始响应（步骤 07 的评测工具）。

**定位**：这是评测脚本，不是运行时代码。它不被 `src/` 导入，不进 `requirements.txt`，
也不读 `src/config.py`——步骤 08 才把选定的适配器写进 `src/integrations/ocr.py`，
并按那时的结论定义正式的环境变量名与 `.env.example` 条目。

**提示词是被测系统的一部分**：同一张图换个提示词，输出格式甚至数字写法都会变
（2026-11-10 实测：要 HTML 表格得到全角逗号 `1，040.00`，要 JSON 得到半角 `1,040.00`）。
所以提示词连同模型一起记进每一份结果文件，换提示词就是换一个被测配置，不能混在一起比。

**默认提示词刻意要求模型「承认读不出来」**：它必须区分「看得见但认不出」(UNREADABLE)、
「单据上没有」(null) 和真实读到的原文，并且**禁止把各行金额相加当成总额**。
这三条直接对应 `src/contracts.py` 的 `FieldStatus` 三态与第 4 条约定
（不把「没认出来」变成数值）。模型做不到就是一条实测结论，不是脚本的缺陷。

**凭据**：从环境变量 `DASHSCOPE_API_KEY` 读取，不接受命令行传入（避免进入进程参数与
shell 历史），也绝不写进任何输出文件（步骤 07.6 的验收条件）。

用法：
    export DASHSCOPE_API_KEY=...
    python tools/ocr_batch.py samples/real/pages --out samples/real/ocr/qwen-vl-ocr
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

DEFAULT_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
"""兜底域名。自 2026-09-30 起不再支持新功能，因此优先用环境变量 `DASHSCOPE_BASE_URL`
给出的工作空间专属域名（`{WorkspaceId}.{region}.maas.aliyuncs.com`，取自百炼控制台
「业务空间管理」页的 API Host 列）。两者认的是同一个 Key，返回的模型列表一致。"""

DEFAULT_PROMPT = """这是一张餐厅采购单据的扫描件。请只识别你在图上真正看到的内容。

以 JSON 输出，结构如下：
{
  "invoice_number": ...,
  "invoice_date": ...,
  "invoice_total": ...,
  "lines": [
    {"index": 1, "item_description": ..., "quantity": ..., "unit": ..., "unit_price": ..., "line_amount": ...}
  ]
}

必须遵守的规则：
1. 每个字段填你在图上看到的原文，保持原样：不要改写数字格式、不要补零、不要加千分位、不要把中文标点换成英文标点。
2. 看得见但认不出来（字迹潦草、涂改、反光、太淡）→ 该字段填字符串 "UNREADABLE"。
3. 单据上根本没有这一项 → 该字段填 null。
4. 不要做任何计算。invoice_total 只能来自单据上印着或写着的那个总额数字；单据上没有总额就填 null，**禁止把各行金额相加后填进去**。
5. 表格里没有填写内容的空白行不要输出。
6. 只输出 JSON，不要解释。"""


class OcrError(RuntimeError):
    pass


def chat_completions_url() -> str:
    base = os.environ.get("DASHSCOPE_BASE_URL", "").strip() or DEFAULT_BASE_URL
    return base.rstrip("/") + "/chat/completions"


IMAGE_MEDIA_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
}
"""扩展名 → data URL 的媒体类型。拆页工具只产 JPEG，但步骤 07 后来并入的
小票样本里有 PNG；媒体类型报错会让服务端拒收或静默按错格式解码，所以按扩展名
显式给出，**不统一写死 image/jpeg**。不认识的扩展名直接跳过，不猜。"""


def recognize(
    image: Path, model: str, prompt: str, api_key: str, timeout: int
) -> dict:
    media_type = IMAGE_MEDIA_TYPES[image.suffix.lower()]
    payload = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:{media_type};base64,"
                            + base64.b64encode(image.read_bytes()).decode()
                        },
                    },
                    {"type": "text", "text": prompt},
                ],
            }
        ],
    }
    request = urllib.request.Request(
        chat_completions_url(),
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )
    started = time.time()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = json.load(response)
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", "replace")[:1000]
        raise OcrError(f"HTTP {error.code}: {detail}") from None
    except urllib.error.URLError as error:
        raise OcrError(f"网络失败：{error.reason}") from None
    elapsed = time.time() - started

    choices = body.get("choices") or []
    if not choices:
        raise OcrError(f"响应里没有 choices：{json.dumps(body, ensure_ascii=False)[:400]}")
    return {
        "image": image.name,
        "model": model,
        "prompt": prompt,
        "elapsed_seconds": round(elapsed, 2),
        "usage": body.get("usage"),
        "request_id": body.get("id"),
        "content": choices[0]["message"]["content"],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="批量识别单页图，逐页落盘原始响应")
    parser.add_argument("images", type=Path, help="含 .jpg 的目录")
    parser.add_argument("--out", type=Path, required=True, help="结果输出目录")
    parser.add_argument("--model", default="qwen-vl-ocr")
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--timeout", type=int, default=240)
    parser.add_argument("--limit", type=int, default=0, help="只跑前 N 张（试水用）")
    parser.add_argument("--redo", action="store_true", help="重跑已有结果")
    args = parser.parse_args(argv)

    api_key = os.environ.get("DASHSCOPE_API_KEY", "").strip()
    if not api_key:
        print("缺少环境变量 DASHSCOPE_API_KEY", file=sys.stderr)
        return 2

    endpoint = chat_completions_url()
    if endpoint.startswith(DEFAULT_BASE_URL):
        print(
            "提示：正在用兜底域名 dashscope.aliyuncs.com（2026-09-30 起不再支持新功能）。"
            "设置 DASHSCOPE_BASE_URL 可改用工作空间专属域名。",
            file=sys.stderr,
        )

    images = sorted(
        path
        for path in args.images.iterdir()
        if path.suffix.lower() in IMAGE_MEDIA_TYPES
    )
    if args.limit:
        images = images[: args.limit]
    if not images:
        suffixes = "/".join(sorted(IMAGE_MEDIA_TYPES))
        print(f"{args.images} 下没有 {suffixes} 文件", file=sys.stderr)
        return 2

    args.out.mkdir(parents=True, exist_ok=True)
    pending = [
        image
        for image in images
        if args.redo or not (args.out / f"{image.stem}.json").exists()
    ]
    print(
        f"共 {len(images)} 张，待识别 {len(pending)} 张，模型 {args.model}\n"
        f"endpoint: {endpoint}"
    )

    lock = threading.Lock()
    done = {"ok": 0, "failed": 0, "tokens": 0}

    def run(image: Path) -> None:
        target = args.out / f"{image.stem}.json"
        try:
            result = recognize(image, args.model, DEFAULT_PROMPT, api_key, args.timeout)
        except OcrError as error:
            target.write_text(
                json.dumps(
                    {"image": image.name, "model": args.model, "error": str(error)},
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            with lock:
                done["failed"] += 1
                print(f"  [失败] {image.name}: {error}", file=sys.stderr)
            return
        target.write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        with lock:
            done["ok"] += 1
            done["tokens"] += (result.get("usage") or {}).get("total_tokens", 0)
            print(f"  [{done['ok'] + done['failed']}/{len(pending)}] {image.name}")

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        list(pool.map(run, pending))

    print(
        f"\n成功 {done['ok']}，失败 {done['failed']}，"
        f"合计 {done['tokens']} tokens -> {args.out}"
    )
    return 1 if done["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
