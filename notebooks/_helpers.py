"""노트북 공용 도우미.

노트북에는 로직을 복사해 두지 않고 `src/` 를 그대로 불러 써요 (그래야 코드가 바뀌어도 노트북이 어긋나지 않아요).
이 파일은 '보여 주기'만 도와주는 얇은 함수 모음이에요: 소스 코드 보여 주기, 긴 글 줄이기, 그림 줄여서 보여 주기,
data/ 가 없을 때 키 없이 만들어 주기.
"""
from __future__ import annotations

import inspect
import io
import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pandas as pd
from IPython.display import Code, Image, Markdown, display

ROOT = next(p for p in [Path.cwd(), *Path.cwd().parents] if (p / "src").is_dir())
DATA = ROOT / "data"


def setup(max_colwidth: int = 90, max_rows: int = 30):
    """표(pandas)를 읽기 좋게 보이도록 설정하고, 저장소 루트를 돌려준다."""
    pd.set_option("display.max_colwidth", max_colwidth)
    pd.set_option("display.max_rows", max_rows)
    pd.set_option("display.width", 160)
    pd.set_option("display.unicode.east_asian_width", True)
    return ROOT


def check_mode(use_api: bool, run_full_eval: bool = False):
    """지금 모드를 크게 보여 준다. 진짜 모드인데 .env 에 키가 없으면 여기서 멈춘다 (키 값은 출력하지 않음)."""
    import os
    if use_api:
        try:
            from dotenv import load_dotenv
            load_dotenv(ROOT / ".env")
        except ImportError:
            pass
        if not os.environ.get("GEMINI_API_KEY"):
            raise RuntimeError("USE_API=True 인데 GEMINI_API_KEY 가 없어요. .env 에 키를 넣거나 USE_API=False 로 바꾸세요.")
        print("🟢 진짜 Gemini 모드 (비용 발생) | 전체 평가:", "실행" if run_full_eval else "건너뜀")
    else:
        print("⚪ 가짜 AI 모드 (키·비용 없음) — 점수는 흐름 확인용이라 의미 없어요")


def ensure_data(use_api: bool = False):
    """data/ (쪽 레코드·청크·색인) 가 없으면 만든다. git 에는 data/ 가 없어서, 저장소를 막 받은 사람은 처음 한 번만 돌아요.
    use_api=False: 키 없이 (--no-vlm --offline: 그림 설명 생략, 임베딩 가짜, 몇 분)
    use_api=True : 진짜 Gemini 로 그림 설명·임베딩까지 (비용 발생, 수십 분)"""
    need = [DATA / "pages.jsonl", DATA / "sources.jsonl", DATA / "figures.jsonl", DATA / "chunks" / "structural.jsonl"]
    if all(p.exists() for p in need):
        print("data/ 가 이미 있어요 (그대로 사용)")
        return
    extra = [] if use_api else ["--no-vlm", "--offline"]
    print("data/ 가 없어", "진짜 Gemini 로" if use_api else "키 없이", "만들어요 ...")
    subprocess.run([sys.executable, "-m", "src.pipeline.build", "--config", "configs/baseline.json", *extra],
                   cwd=ROOT, check=True)


def show_source(*objs, max_lines: int | None = None):
    """함수·클래스의 실제 소스를 보여 준다 (inspect.getsource). 너무 길면 앞부분만."""
    parts = []
    for o in objs:
        src = textwrap.dedent(inspect.getsource(o))
        lines = src.split("\n")
        if max_lines and len(lines) > max_lines:
            src = "\n".join(lines[:max_lines]) + f"\n    ... (이하 {len(lines) - max_lines}줄 생략, 전체는 {inspect.getsourcefile(o).replace(str(ROOT), '').lstrip(chr(92) + '/')})"
        parts.append(src)
    display(Code("\n\n".join(parts), language="python"))


def short(text: str, n: int = 200) -> str:
    text = (text or "").replace("\n", " ⏎ ")
    return text if len(text) <= n else text[:n] + f"… (+{len(text) - n}자)"


def pprint_text(text: str, n: int = 600):
    """긴 글을 앞부분만 출력."""
    t = text or ""
    print(t if len(t) <= n else t[:n] + f"\n… (+{len(t) - n}자 생략)")


def thumb(path, width: int = 420):
    """그림을 작게 줄여 보여 준다 (노트북 파일이 커지지 않게)."""
    from PIL import Image as PILImage
    im = PILImage.open(path).convert("RGB")
    if im.width > width:
        im = im.resize((width, int(im.height * width / im.width)))
    buf = io.BytesIO()
    im.save(buf, format="PNG", optimize=True)
    return Image(data=buf.getvalue())


def read_jsonl(path):
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            if line.strip():
                out.append(json.loads(line))
    return out


def md(text: str):
    display(Markdown(text))


def md_table_to_df(md_text: str) -> pd.DataFrame:
    """Markdown 표 글(`| a | b |` 형식)을 pandas 표로 바꿔서 보기 좋게 보여 준다 (표시용)."""
    lines = [l for l in md_text.split("\n") if l.strip().startswith("|")]
    rows = [[c.strip() for c in l.strip().strip("|").split("|")] for l in lines if not set(l.replace("|", "").strip()) <= set("-: ")]
    head, body = rows[0], rows[1:]
    head = [h if h else f"(빈 머리글{i})" for i, h in enumerate(head)]
    return pd.DataFrame(body, columns=head)
