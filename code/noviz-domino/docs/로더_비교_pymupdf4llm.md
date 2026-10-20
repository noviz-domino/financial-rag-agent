# 로더 비교: 우리 로더(pdfplumber) vs pymupdf4llm (2026-10-20)

## 설치·보안 정책
- `pip install pymupdf4llm` 은 **빈 패키지(2.0)** 만 깔려요. 2.x부터 코드가 `pymupdf` 본체로 합쳐졌어요 → `pip install pymupdf` (pymupdf 2.0 + pymupdf_layout + tabulate)
- 이 PC에서 **차단 없이 실행됨** (import·변환·레이아웃 모델 모두 OK)
- 레이아웃 모드는 RapidOCR를 자동으로 불러요. shapely 2.2.0 도 이제 로딩되지만, **기본 모델이 중국어·영어라 한글을 `言号`처럼 읽음** → `use_ocr=False` 로 끔
- 모드 2가지: `layout`(AI 레이아웃 모델, 348쪽 약 4~5분) / `legacy`(규칙 기반, 약 1분)

## 문제 페이지 3곳 (눈으로 비교)
| 페이지 | 우리 로더 | pymupdf4llm layout | legacy |
|---|---|---|---|
| 가이드북 10쪽 (펼침면) | 좌→우 순서 정확, 표 머리글이 캡션으로 밀림 | 표 머리글 정확, **"2단계" 제목이 1단계 본문보다 먼저** | 표 `기간` 열 누락, 줄마다 빈 줄 |
| 자영업자 2쪽 (2단+차트+표) | 표 lossy(평문), x축 라벨 `24.12/54.1/4` 섞임(PDF에서 라벨이 겹쳐 찍힘) | **표 Markdown 깔끔**, 제목 `#`, 차트 글자 묶음 | 차트 숫자가 줄마다 흩어짐 |
| 주택연금 72쪽 (단계표+세로 라벨) | **`[구분: 가입가능/가입불가]` 연결** | 표를 그림으로 보고 `가/입/가/능` 한 글자씩 흩어짐 | 세로 글자가 따로 떨어짐 |

## 점수
글자 보존(한글+숫자 기준): 우리 92~101%, layout 91~101%, **legacy 35~102% (보험약관 35%, 가이드북 66%) → 탈락**

BM25 페이지 Hit (가이드북+추가 80문항, `python -m src.check_loader pages_p4l_layout.jsonl pages_v3.jsonl`)
| 로더 | Hit@1 | Hit@3 | Hit@5 |
|---|---|---|---|
| 기본 추출 | 0.613 | 0.762 | 0.825 |
| 우리 로더 v2 | 0.625 | 0.787 | **0.850** |
| pymupdf4llm layout | 0.600 | **0.812** | 0.838 |
| 합친 로더 v3 | 0.613 | 0.775 | 0.838 |

→ 1문항(0.0125) 차이라 BM25로는 **비슷함**.

청크 단위 (같은 청킹 규칙, 로더만 교체, `data/chunks/loader_compare/compare_bm25_loader.json`)
| 청킹 | 로더 | 가이드 H@5 | 추가 H@5 | 추가 MRR | LLM 입력 글자(top5) |
|---|---|---|---|---|---|
| structural_1200 | v2 / p4l / v3 | 0.90 / 0.90 / 0.90 | 0.900 / 0.917 / 0.900 | 0.746 / 0.756 / 0.750 | 3,067 / 3,408 / 2,921 |
| parentchild_400 [parent] | v2 / p4l / v3 | 0.95 / 0.95 / 1.00 | 0.850 / 0.867 / 0.850 | 0.732 / 0.736 / 0.739 | 13,022 / 22,028 / 12,911 |
| recursive_500_50 | v2 / p4l / v3 | 0.85 / 0.85 / 0.85 | 0.883 / 0.833 / 0.883 | 0.748 / 0.704 / 0.702 | 2,259 / 2,218 / 2,241 |

→ 차이는 1~2문항. pymupdf4llm 단독은 펼침면을 안 나눠 parent 입력이 22,028자로 커짐. v3는 점수를 유지하면서 lossy 표를 줄임

## 합친 결과 (v3 = `python -m src.loader --merge-p4l` → `pages_v3.jsonl`)
- **제목**: pymupdf4llm이 `#`으로 본 줄에 `## ` 표시 (511줄) → 청킹 단락 경계. 단계(#/##/###)는 문서마다 들쭉날쭉해서 안 씀
- **표**: 우리 lossy 표 → 같은 쪽 pymupdf4llm 표가 한글 재현 90%·숫자 토큰 재현/정밀 95%·글자 정밀 80% 이상이면 대체. 세로 라벨 표는 제외
  - **lossy 134 → 33** (101개 대체) → Gemini 재판독 대상이 그만큼 줄어듦(비용 절감)
  - 시행착오: 숫자를 "글자"로 세면 `247,738` → `24 | 7,738` 칸 깨짐을 통과시킴 → 숫자 "토큰"으로 변경. `<sup>` 태그만 지우면 `16.5²)` → `16.52)` 오염 → 숫자뿐인 위첨자는 내용째 삭제
- 그대로 둔 것: 읽는 순서·펼침면·세로 라벨·그림 처리(우리 로더가 더 정확)

## 아직 안 한 것
- v3를 기본 입력으로 바꿀지 결정 (청크 점수 + 임베딩 검색 확인 후). 지금 기본은 여전히 `pages_v2.jsonl`
- `figures.py`는 아직 v2의 lossy 표 기준 → v3로 바꾸면 Gemini 대상 표가 줄어듦

## 명령
```bash
python -m src.compare_p4l                     # 두 모드 변환 → pages_p4l_{layout,legacy}.jsonl
python -m src.loader --merge-p4l              # → pages_v3.jsonl
python -m src.check_loader pages_p4l_layout.jsonl pages_v3.jsonl
python -m src.chunking --strategy structural --pages pages_v3.jsonl   # → structural_1200_v3
```
