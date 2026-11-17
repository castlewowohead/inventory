#!/usr/bin/env bash
# 步骤 07.6 的多模型对照：同一组图、同一提示词，跑完全部候选型号。
#
# 「同一提示词」是这里的要害。提示词是被测配置的一部分（ocr_batch.py 顶部有说明），
# 换提示词就是换一个被测配置；本脚本不传 --prompt，四个型号一律用 ocr_batch.py 的
# DEFAULT_PROMPT，结果文件里各自记着它的内容，事后可以核对是不是同一份。
#
# 凭据从 backend/.env 读进环境变量再传给子进程，不出现在命令行参数里
# （命令行参数会进 ps 和 shell 历史——07.6 的验收条件是密钥不进样本、日志或仓库）。
#
# 用法：bash tools/run_ocr_matrix.sh

set -u

cd "$(dirname "$0")/.." || exit 1

if [ ! -f .env ]; then
  echo "缺少 backend/.env" >&2
  exit 2
fi
set -a
# shellcheck disable=SC1091
. ./.env
set +a

PY=./.venv/Scripts/python.exe
[ -x "$PY" ] || PY=python

# 型号 → 输出目录名。vanchin/deepseek-ocr 带斜杠，不能直接当目录名。
MODELS="qwen-vl-ocr:qwen-vl-ocr qwen3.5-ocr:qwen3.5-ocr qwen3-vl-plus:qwen3-vl-plus vanchin/deepseek-ocr:deepseek-ocr"

# 两组图：扫描单据单页图，以及后来并入的小票样本。输出到同一个型号目录下，
# 文件名本身带来源前缀，不会撞。
IMAGE_DIRS="samples/real/pages samples/real/receipts"

for entry in $MODELS; do
  model="${entry%%:*}"
  slug="${entry##*:}"
  for dir in $IMAGE_DIRS; do
    echo ""
    echo "=============== $model <- $dir ==============="
    "$PY" -X utf8 tools/ocr_batch.py "$dir" \
      --out "samples/real/ocr/$slug" \
      --model "$model" \
      --workers 4 \
      --timeout 300
  done
done

echo ""
echo "=============== 全部完成 ==============="
for entry in $MODELS; do
  slug="${entry##*:}"
  n=$(ls "samples/real/ocr/$slug" 2>/dev/null | wc -l)
  bad=$(grep -l '"error"' "samples/real/ocr/$slug"/*.json 2>/dev/null | wc -l)
  echo "$slug: $n 份结果，其中 $bad 份是失败记录"
done
