#!/usr/bin/env bash
# 노트북이 지금 코드(src/)로 끝까지 도는지 확인해요. 가짜 AI 모드라 키·비용이 없어요.
# src/ 를 바꾼 뒤 PR 올리기 전에 실행하세요:  bash scripts/check_notebook.sh
# 실행 결과는 임시 파일에만 저장돼서 노트북 파일은 바뀌지 않아요.
set -e
cd "$(dirname "$0")/.."
PY=.venv/Scripts/python; [ -x "$PY" ] || PY=.venv/bin/python; [ -x "$PY" ] || PY=python
OUT=$(mktemp -d)
NB_USE_API=0 "$PY" -m jupyter nbconvert --to notebook --execute \
  --ExecutePreprocessor.timeout=1800 --output-dir "$OUT" \
  "notebooks/금융_RAG_Agent_전체실행.ipynb"
echo "✅ 노트북이 지금 코드로 끝까지 실행됐어요 (가짜 AI 모드)."
rm -rf "$OUT"
