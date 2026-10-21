"""노트북 공용 도우미 — 결과를 '보기 좋게 보여 주는' 보조 함수 모음.

이 파일에는 RAG 로직(PDF 읽기·청킹·검색·답변·평가)이 하나도 없어요. 그건 전부 `src/` 에 있어요.
노트북에 로직을 복사해 두지 않고 `src/` 를 그대로 불러 쓰기 때문에, 코드가 바뀌어도 노트북이 어긋나지 않아요.
여기 있는 건 노트북 칸마다 반복되는 '출력용 코드'를 한곳에 모아 둔 것뿐이에요.

노트북 첫 칸의 `from _helpers import *` 가 아래 함수들을 모두 불러와요.

    준비      setup()          표(pandas) 출력 모양 설정
              check_mode()     진짜/가짜 모드 표시, 진짜 모드인데 키가 없으면 멈춤
              ensure_data()    data/ 가 없으면 빌드를 대신 실행
    코드 보기 show_source()    src/ 의 실제 함수 코드를 노트북에 보여 줌
    글 출력   short()          긴 글을 한 줄로 줄인 문자열로 (표 안에 넣을 때)
              pprint_text()    긴 글을 앞부분만 출력
              md()             Markdown 서식(굵게·표 등)으로 출력
    그림      thumb()          그림을 작게 줄여 표시
    파일·표   read_jsonl()     .jsonl 결과 파일을 읽어 리스트로
              md_table_to_df() Markdown 표 글을 pandas 표로

이름 앞의 `_` 는 "내부용 파일이라 공부할 대상은 아니에요"라는 파이썬 관례예요.
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

# 저장소 맨 위 폴더(= src/ 가 들어 있는 폴더). 노트북을 어느 폴더에서 열어도 같은 곳을 가리켜요.
ROOT = next(p for p in [Path.cwd(), *Path.cwd().parents] if (p / "src").is_dir())
# 빌드 결과(쪽 레코드·청크·색인·캐시)가 저장되는 폴더. git 에는 올라가지 않아요.
DATA = ROOT / "data"


# ─────────────────────────── 준비 ───────────────────────────

def setup(max_colwidth: int = 90, max_rows: int = 30):
    """[하는 일] pandas 표가 노트북에서 읽기 좋게 보이도록 출력 설정을 바꾼다.

    - max_colwidth: 칸 하나에 보여 줄 최대 글자 수 (넘으면 … 으로 줄임)
    - max_rows    : 표에서 보여 줄 최대 행 수
    - 한글은 영문보다 폭이 넓어서 east_asian_width 를 켜야 표 칸이 어긋나지 않아요.
    [돌려주는 값] 저장소 맨 위 폴더(ROOT)
    """
    pd.set_option("display.max_colwidth", max_colwidth)
    pd.set_option("display.max_rows", max_rows)
    pd.set_option("display.width", 160)
    pd.set_option("display.unicode.east_asian_width", True)
    return ROOT


def check_mode(use_api: bool, run_full_eval: bool = False):
    """[하는 일] 지금 노트북이 진짜 Gemini 모드인지 가짜 AI 모드인지 크게 보여 준다.

    - use_api=True 인데 .env 에 GEMINI_API_KEY 가 없으면 에러로 멈춰요.
      (뒤쪽 칸까지 가서 실패하지 않고 첫 칸에서 바로 알 수 있게)
    - 키 값 자체는 절대 출력하지 않아요. 있는지 없는지만 봐요.
    - run_full_eval 은 10장의 180문항 전체 평가를 돌릴지 여부예요 (표시만 해요).
    """
    import os
    if use_api:
        try:
            from dotenv import load_dotenv      # .env 파일 내용을 환경 변수로 불러오기
            load_dotenv(ROOT / ".env")
        except ImportError:
            pass
        if not os.environ.get("GEMINI_API_KEY"):
            raise RuntimeError("USE_API=True 인데 GEMINI_API_KEY 가 없어요. .env 에 키를 넣거나 USE_API=False 로 바꾸세요.")
        print("🟢 진짜 Gemini 모드 (비용 발생) | 전체 평가:", "실행" if run_full_eval else "건너뜀")
    else:
        print("⚪ 가짜 AI 모드 (키·비용 없음) — 점수는 흐름 확인용이라 의미 없어요")


def ensure_data(use_api: bool = False):
    """[하는 일] data/ (쪽 레코드·청크·색인) 가 없으면 빌드 명령을 대신 실행해서 만든다.

    git 에는 data/ 가 없어서, 저장소를 막 받은 사람은 처음 한 번만 이 단계가 돌아요.
    이미 있으면 아무것도 안 하고 그대로 써요 (다시 만들고 싶으면 data/ 를 지우고 실행).
    - use_api=False: 키 없이 만들어요 (--no-vlm --offline: 그림 설명 생략, 임베딩 가짜). 몇 분.
    - use_api=True : 진짜 Gemini 로 그림 설명·임베딩까지 만들어요. 비용 발생, 수십 분.
    """
    need = [DATA / "pages.jsonl", DATA / "sources.jsonl", DATA / "figures.jsonl", DATA / "chunks" / "structural.jsonl"]
    if all(p.exists() for p in need):
        print("data/ 가 이미 있어요 (그대로 사용)")
        return
    extra = [] if use_api else ["--no-vlm", "--offline"]
    print("data/ 가 없어", "진짜 Gemini 로" if use_api else "키 없이", "만들어요 ...")
    subprocess.run([sys.executable, "-m", "src.pipeline.build", "--config", "configs/baseline.json", *extra],
                   cwd=ROOT, check=True)


# ─────────────────────────── 코드 보기 ───────────────────────────

def show_source(*objs, max_lines: int | None = None):
    """[하는 일] src/ 에 있는 함수·클래스의 실제 코드를 노트북에 색깔 입혀 보여 준다.

    노트북에 코드를 복사해 두면 src/ 가 바뀔 때 어긋나니까, 매번 실제 파일에서 읽어 와요.
    - objs     : 보여 줄 함수·클래스 (여러 개 가능). 예) show_source(chunk_glossary, chunk_terms)
    - max_lines: 이 줄 수를 넘으면 앞부분만 보여 주고 파일 위치를 알려 줘요.
    """
    parts = []
    for o in objs:
        src = textwrap.dedent(inspect.getsource(o))
        lines = src.split("\n")
        if max_lines and len(lines) > max_lines:
            src = "\n".join(lines[:max_lines]) + f"\n    ... (이하 {len(lines) - max_lines}줄 생략, 전체는 {inspect.getsourcefile(o).replace(str(ROOT), '').lstrip(chr(92) + '/')})"
        parts.append(src)
    display(Code("\n\n".join(parts), language="python"))


# ─────────────────────────── 글 출력 ───────────────────────────

def short(text: str, n: int = 200) -> str:
    """[하는 일] 긴 글을 한 줄짜리 짧은 문자열로 만든다 (출력하지 않고 돌려주기만 해요).

    표 칸 안에 청크 내용을 넣을 때처럼 '줄바꿈 없이 짧게' 보여야 할 때 써요.
    - 줄바꿈은 ⏎ 로 바꿔서 한 줄로 만들어요.
    - n 글자를 넘으면 잘라 내고 몇 글자 남았는지 붙여요.
    """
    text = (text or "").replace("\n", " ⏎ ")
    return text if len(text) <= n else text[:n] + f"… (+{len(text) - n}자)"


def pprint_text(text: str, n: int = 600):
    """[하는 일] 긴 글을 앞부분 n 글자만 그대로(줄바꿈 유지) 출력한다.

    쪽 텍스트·청크·답변처럼 긴 글을 통째로 찍으면 노트북이 너무 길어져서 앞부분만 보여 줘요.
    """
    t = text or ""
    print(t if len(t) <= n else t[:n] + f"\n… (+{len(t) - n}자 생략)")


def md(text: str):
    """[하는 일] 글을 Markdown 서식으로 출력한다 (굵게·목록·표가 서식대로 보여요).

    예) md("**검색 결과** 5개") → 굵은 글씨로 표시
    """
    display(Markdown(text))


# ─────────────────────────── 그림 ───────────────────────────

def thumb(path, width: int = 420):
    """[하는 일] 그림 파일을 가로 width 픽셀로 줄여서 노트북에 표시한다.

    원본 그림을 그대로 넣으면 노트북 파일(.ipynb)이 수십 MB 로 커져서 git 에 올리기 힘들어요.
    줄인 PNG 만 노트북에 저장돼요. 원본 파일은 그대로 둬요.
    """
    from PIL import Image as PILImage
    im = PILImage.open(path).convert("RGB")
    if im.width > width:
        im = im.resize((width, int(im.height * width / im.width)))
    buf = io.BytesIO()
    im.save(buf, format="PNG", optimize=True)
    return Image(data=buf.getvalue())


# ─────────────────────────── 파일·표 ───────────────────────────

def read_jsonl(path):
    """[하는 일] .jsonl 파일(한 줄에 JSON 하나)을 읽어 dict 리스트로 돌려준다.

    이 프로젝트의 결과 파일(pages.jsonl, chunks/*.jsonl, runs/*/results.jsonl 등)이 모두 이 형식이에요.
    빈 줄은 건너뛰어요.
    """
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            if line.strip():
                out.append(json.loads(line))
    return out


def md_table_to_df(md_text: str) -> pd.DataFrame:
    """[하는 일] Markdown 표 글(`| a | b |` 형식)을 pandas 표로 바꾼다 (보여 주기용).

    표 추출 결과는 청크 안에 Markdown 글로 저장돼 있어서, 그대로 찍으면 | 기호가 가득한 글로 보여요.
    pandas 표로 바꾸면 칸이 맞춰져서 병합 칸이 제대로 채워졌는지 눈으로 확인하기 쉬워요.
    - `|---|---|` 같은 구분 줄은 빼요.
    - 머리글 칸이 비어 있으면 "(빈 머리글N)" 으로 이름을 붙여요 (pandas 는 빈 열 이름을 싫어해요).
    """
    lines = [l for l in md_text.split("\n") if l.strip().startswith("|")]
    rows = [[c.strip() for c in l.strip().strip("|").split("|")] for l in lines if not set(l.replace("|", "").strip()) <= set("-: ")]
    head, body = rows[0], rows[1:]
    head = [h if h else f"(빈 머리글{i})" for i, h in enumerate(head)]
    return pd.DataFrame(body, columns=head)
