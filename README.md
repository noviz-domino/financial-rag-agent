# financial-rag-agent

금융 PDF 23개(용어집·표준약관·금융생활 가이드북·정책 자료)를 검색해 **근거(파일명·쪽)와 함께 답하는 RAG Agent**와, 그 성능을 숫자로 재는 **평가 루프**예요.

- 답할 수 없는 질문은 지어내지 않고 **되묻기·자료 없음·거절·전제 바로잡기** 중 알맞은 대응을 골라요.
- 같은 질문 180개로 **실행 → 채점 → 실패 원인 분석 → 설정 수정 → 재실행**을 반복해요.
- 청킹·검색 방식·기본 RAG와 Agent를 **한 번에 한 가지씩 바꿔** 비교해요.

> **개발 배경**: 세 명이 각자 구현·실험한 결과를 원본 PDF와 나란히 대조해, 기능마다 더 나은 쪽의 아이디어와 장점만 골라 하나로 합쳤어요. 결정 근거는 [조율 결과](docs/plans/조율_결과.md)에 있어요.

---

## 목차
1. [빠른 시작](#1-빠른-시작)
2. [전체 흐름](#2-전체-흐름)
3. [단계별 처리 기준](#3-단계별-처리-기준) ← **팀원이 꼭 알아야 할 바뀐 기준**
4. [평가](#4-평가)
5. [실험](#5-실험)
6. [현재 상태와 알려진 한계](#6-현재-상태와-알려진-한계)
7. [폴더 구조](#7-폴더-구조)
8. [협업 방식](#8-협업-방식)
9. [문서](#9-문서)

---

## 1. 빠른 시작

모든 명령은 **Git Bash**, 저장소 맨 위 폴더에서 실행해요.

```bash
git switch main && git pull                          # 최신 main 받기
python -m venv .venv && source .venv/Scripts/activate
pip install -r requirements.txt -r requirements-dev.txt
cp .env.example .env                                 # .env 를 열어 GEMINI_API_KEY 입력
pytest                                               # API 를 쓰지 않는 테스트 (78개)
```

### 노트북으로 보기 (추천)
[`notebooks/금융_RAG_Agent_전체실행.ipynb`](notebooks/금융_RAG_Agent_전체실행.ipynb) 한 파일을 위에서 아래로 실행하면 PDF 읽기부터 평가·결과 분석까지 전 과정을 단계별 설명과 함께 볼 수 있어요.

| 맨 위 설정 | 동작 |
|---|---|
| `USE_API = True` (기본) | 진짜 Gemini 사용 (**비용 발생**) |
| `USE_API = False` | 가짜 AI로 **코드가 끝까지 도는지만** 확인 (키·비용 없음, 점수 의미 없음) |
| `RUN_FULL_EVAL = True` | 180문항 전체 평가까지 (기본 꺼짐) |

### 명령어로 실행
```bash
python -m src.pipeline.build --config configs/baseline.json --variants all   # PDF → 청크 → 색인 (처음 한 번, 수십 분)
python -m src.sources.check                                                  # 정답 근거 누락 확인 (0이어야 함)
python -m src.evaluation.run --config configs/baseline.json --sets positive,negative,life_guide,additional
python -m src.evaluation.run --config configs/exp3_agent.json --sets positive,negative,life_guide,additional
```

**자주 쓰는 옵션**

| 옵션 | 뜻 |
|---|---|
| `--config 파일` | 어떤 설정(기준·실험)으로 돌릴지 |
| `--sets a,b` | 돌릴 질문 세트 (`positive`, `negative`, `life_guide`, `additional`, `golden`) |
| `--limit N` | 세트마다 앞 N문항만 (시험용) |
| `--retrieval-only` | 검색 지표만 계산, 답변 생성 안 함 (싸고 빠름) |
| `--no-vlm` | 그림·표 이미지를 Gemini로 설명하는 단계 생략 |
| `--offline` / `--dry-run` | 빌드 / 평가를 가짜 AI로 (키 없이 코드 점검용) |
| `--force` | 같은 실험 ID 결과가 있어도 덮어쓰기 |

> Gemini 응답·임베딩은 `data/cache/`에 저장돼서 **같은 내용은 다시 돌려도 비용이 들지 않아요.** 청크 글자가 한 글자라도 바뀌면 그 청크만 새로 임베딩해요.

---

## 2. 전체 흐름

```
[준비]  PDF 23개 → 읽기(펼침면·2단·노이즈 정리) → 표 정리 · 그림을 글로 → 정답 근거 연결
                 → 청킹(용어·조항·제목 단위) → 임베딩 → Chroma + BM25 색인

[실행]  질문 → 기본 RAG : 검색 → 근거 판단 → 답변
             → Agent    : 판단(route) → 검색 → 근거 충분? → (부족하면 검색어 바꿔 재검색, 최대 2번)
                          → 답변 → 인용·숫자 검사 → (실패하면 재작성 1번)

[평가]  답변·인용·대응 행동 저장 → 채점 → 실패 원인 분류 → 실험끼리 비교
```

### 질문 세트와 검색 범위
| 세트 | 문항 | 검색 범위 (`doc_type`) |
|---|---|---|
| 기본 positive | 60 | `glossary` + `terms` |
| 기본 negative | 40 | `glossary` + `terms` |
| 금융생활 positive | 20 | `life_guide` |
| 추가 문서 positive | 60 | `additional` (보험약관 예시도 여기) |
| 골든셋 (직접 작성) | 18 | 문항마다 |

검색할 때 범위를 빠뜨리면 **실행이 멈춰요**(`ScopeRequiredError`). 세트마다 "답할 수 있는 범위"가 달라서예요.

---

## 3. 단계별 처리 기준

라이브러리를 기본값 그대로 쓰지 않고 이 문서들에 맞게 바꾼 부분이에요. 모든 숫자는 [`configs/baseline.json`](configs/baseline.json)에서 바꿀 수 있어요.

### 3-1. PDF 읽기 — pdfplumber + 직접 만든 규칙 ([`src/loading/loader.py`](src/loading/loader.py))
pdfplumber 기본 `extract_text()`는 쪽 전체를 위→아래로 읽어서, 펼침면·2단 문서에서 문장이 섞여요. 그래서 글자 좌표를 보고 아래 규칙을 더했어요.

| 문제 | 규칙 | 기준값 |
|---|---|---|
| 펼침면(A4 두 장이 한 쪽)에서 좌우가 섞임 | 좌/우 반쪽으로 나눠 읽기 (`page` 그대로, `half`=L/R) | 가로 > 세로 × **1.15** |
| 2단 본문이 가로로 섞임 | 가운데 여백을 가로지르는 줄이 적으면 2단 → 좌단 먼저 | 가로지르는 줄 **30% 미만** |
| 굵은 글씨 겹침 `1100..55%%` | 겹친 글자 제거 → `10.5%` | – |
| 각주 위첨자 `16.5²)` → `16.52)` | 본문보다 아주 작은 숫자 삭제 | 본문 글자의 **0.65** 미만 |
| 세로쓰기 라벨이 흩어짐 | `[세로] 가입불가`처럼 한 줄로 | 1~2글자 4개 이상 세로 연속 |
| 머리말·꼬리말 반복 | 위·아래 3줄에서 반복되는 줄 삭제 | **30% 이상** 쪽, 3번 이상 |
| 쪽 번호가 본문에 섞임 | 위·아래 여백의 1~3자리 숫자 삭제 | – |
| 글자 깨짐 `(cid:123)` | 줄 앞은 `•`, 한글 사이는 `·`, 나머지 삭제 | – |

**쪽 번호 기준**: 이 프로젝트의 "쪽"은 모두 **PDF 쪽 순서(1부터)**예요. 인쇄된 쪽 번호가 아니에요. 답변 출처 `(파일명 p.N)`도 같아요.

### 3-2. 표 ([`src/tables/extract.py`](src/tables/extract.py))
| 단계 | 내용 |
|---|---|
| 찾기 | pdfplumber `find_tables`를 두 설정으로 시도: 기본(선 있는 표) → 세로선만 있는 표 |
| 손실 판정 | 원문 대비 **한글·숫자를 따로** 세어 하나라도 **90% 미만**이면 손실 |
| 손실이면 | 합쳐진 칸의 글자를 걸친 칸 모두에 채우고, 비어 있는 행 머리글은 위 행 것을 반복 |
| 그래도 손실이면 | Gemini가 표 이미지를 다시 읽음 |
| 긴 표 | 행 단위로 나누고 머리글 반복 (1,200자) |

→ 결과: 단순 표 56 · 칸을 채운 표 105 · Gemini로 넘긴 표 30

### 3-3. 그림 ([`src/figures/`](src/figures))
좌표로 그림 후보를 찾고 → 배너·배경·QR을 규칙으로 거르고 → Windows OCR(2배 확대)로 글자를 미리 읽은 뒤 → **Gemini(gemini-2.5-flash)가 종류를 먼저 고르고 그 종류에 맞게** 글로 바꿔요.

| 종류 | 설명 방식 |
|---|---|
| 숫자 그래프·표·절차도 | 값을 **전부** 표 형식으로 |
| 화면 캡처·일러스트 | **짧게 요약**, 안 보이는 글자는 쓰지 않음 |
| 장식 | 청크 만들지 않음 |

설명 속 숫자가 원문·OCR에 없으면 **"인용 주의"**를 붙이고, 답변에서 그 숫자는 단정하지 않아요. 설명이 비정상적으로 길면(반복 고장) 걸러요.

### 3-4. 청킹 — 구조형이 기준 ([`src/chunking/structural.py`](src/chunking/structural.py))
| 문서 | 자르는 방법 |
|---|---|
| 용어집 | 용어 1개 = 청크 1개 |
| 약관 | 조항 1개 = 청크 1개. 1,200자가 넘으면 **①②항 경계에서만** 나누고 조 제목을 반복. 제목만 있는 조각은 만들지 않음 |
| 가이드북·추가 | 글꼴 크기로 찾은 제목 단위. **100자 미만** 짧은 절은 이웃과 합침. 섹션 경로(문서 > 장 > 절) 저장 |
| 표·그림 | 본문과 따로 청크. 그림이 표를 60% 이상 덮으면 표는 빼서 중복 방지 |

청크마다 **정답 근거 ID**(채점용), **parent_id**(조항 전체·반쪽 페이지), **링크**(앞뒤 청크, "제7조에 따라" 같은 조항 참조, "그림 Ⅲ-9" 같은 그림 참조)가 붙어요. 기본 RAG는 청크만 쓰고, parent·링크는 Agent가 필요할 때만 펼쳐요.

→ 결과: 1,508개 (용어집 228 · 약관 407 · 가이드북 144 · 추가 729), **정답 근거 누락 0**

### 3-5. 임베딩·검색 ([`src/index/`](src/index), [`src/retrieval/`](src/retrieval))
| 항목 | 값 |
|---|---|
| 임베딩 | gemini-embedding-001, 768차원, 청크의 `text` (제목 접두어 없음) |
| 저장 | Chroma (청킹 방식별로 다른 색인 폴더) |
| 단어 검색 | Kiwi 형태소 분석 + BM25 |
| 합치기 | 하이브리드: 두 결과의 순위를 RRF(60)로 합침 |
| 가져오는 개수 | k = 5 |

### 3-6. 답변 ([`src/generation/`](src/generation), [`src/agent/`](src/agent))
- 모델: gemini-2.5-flash, temperature 0
- **근거 판단 → 답변** 2단계, 둘 다 JSON 출력
- 대응 행동 7가지: `answer` · `answer_qualified` · `correct` · `clarify` · `abstain` · `defer_freshness` · `refuse`
- 질문 속 "이전 지시를 무시하고…" 같은 **끼워 넣은 지시**는 걸러 낸 질문(`clean_query`)으로 검색
- 근거에 없는 숫자 금지, 투자 권유 금지
- Agent 끝내는 조건: 재검색 최대 2번, 재작성 최대 1번, AI 호출 최대 8번. 구조도는 [`docs/agent_graph.md`](docs/agent_graph.md)

---

## 4. 평가

**검색이 틀렸는지, 답변이 틀렸는지를 나눠서** 채점해요.

| 구분 | 지표 |
|---|---|
| 검색 | Hit@1·Hit@5, Recall@5, MRR (정답 근거가 있는 148문항) |
| 답변 | 답변 점수 0~2 (채점 AI: gemini-2.5-pro), 필수 내용 포함률, 금지 내용 위반 |
| 대응 | 행동 일치율 (7가지·5묶음), 과잉 거절률, 지시 끼워 넣기 대응률 |
| 근거 | 인용 정확도 |
| 비용 | AI 호출 수, 검색 횟수, 입력·출력 토큰, 시간 |

**실패 원인**: 검색 실패 / 생성 오류 / 잘못된 거절 / 행동 불일치 / 환각 / 정답 없음

**결과 저장**
```
runs/{실험ID}/config.json     설정 + 기준 대비 바뀐 항목
              results.jsonl   문항별 답변·검색한 청크·인용·시간·토큰
              scores.jsonl    문항별 점수·실패 원인
              summary.json    세트별 평균·총비용
runs/experiments.jsonl        실험 목록
```

---

## 5. 실험

기준 설정에서 **한 가지만** 바꿔 비교해요. 실험 설정 파일에는 바뀐 항목만 적혀 있어요.

| 실험 | 설정 파일 | 바꾸는 것 |
|---|---|---|
| 기준 | `baseline.json` | 구조형 청킹 · 하이브리드 k=5 · 기본 RAG |
| 0 흔들림 | `exp0_repeat.json` | 기준을 한 번 더 (이 차이보다 작으면 "효과 없음") |
| 1a 청킹 | `exp1a_recursive_500_50.json` | 글자 수 자르기 (500자, 겹침 50) |
| 1b 청킹 | `exp1b_parentchild.json` | parent-child (child 400자로 찾고 parent로 묶음) |
| 2a 검색 | `exp2a_vector.json` | 뜻 검색(벡터)만 |
| 2b 검색 | `exp2b_bm25.json` | 단어 검색(BM25)만 |
| 3 구조 | `exp3_agent.json` | 기본 RAG → Agent |
| 4 문맥 | `exp4_embed_prefix.json` | 임베딩에 `[문서 > 장 > 절]` 붙이기 |
| 마지막 | – | 설정 확정 후 골든셋으로 1번 |

---

## 6. 현재 상태와 알려진 한계

**됨**: 전체 코드, 테스트 78개 통과, 정답 근거 누락 0, BM25 검색 Hit@5 0.851 (키 없이 측정)
**진행 중**: 실제 Gemini로 빌드·시험 평가 · **할 일**: 실험 0~4 전체 실행, 골든셋 18문항 작성·검수

**알려진 한계** (고칠 예정)
- 각주 위첨자 기준 0.65가 작은 글씨로 적힌 진짜 숫자도 지워요 (예: 출처 줄 "2024년" → "년"). 0.60 이하로 낮출 예정
- 글자 수 자르기의 겹침 50자가 PDF 한 줄(약 100자)보다 짧아 실제로는 겹치지 않아요. 실험 1a 해석 때 주의
- 그림 숫자 검사는 OCR이 같이 틀린 값을 잡지 못해요 → 의심 그림은 사람 확인
- 대응 행동 매핑 중 애매한 3건(NEG009·034·037)은 팀 확인 필요

---

## 7. 폴더 구조

```
financial-rag-agent/
├─ 01_data/  02_questions/  03_answers/   원본 PDF·질문·기준 답안 (수정 금지)
├─ 05_golden/                             직접 만드는 골든셋
├─ src/
│  ├─ loading/  tables/  figures/         PDF 읽기 · 표 · 그림
│  ├─ sources/  chunking/                 정답 근거 연결 · 청킹
│  ├─ index/  retrieval/                  임베딩·색인 · 검색 도구
│  ├─ generation/  agent/                 기본 RAG · LangGraph Agent
│  ├─ evaluation/                         평가 루프·지표
│  ├─ common/  pipeline/                  설정·스키마·Gemini 클라이언트 · 빌드 명령
├─ configs/                               기준·실험 설정, 대응 행동 매핑
├─ notebooks/                             전체 실행 노트북
├─ tests/                                 테스트 (API 안 씀)
├─ docs/                                  기획서·조율 결과·과제 안내·Agent 구조도
├─ data/  runs/                           생성 결과·실행 기록 (git 제외)
└─ code/noviz-domino/  chunks/jjh7757/    통합 전 참고 자료 (지금 코드와 별개)
```

---

## 8. 협업 방식

```bash
git switch main && git pull                 # 1. 최신 main 받기
git switch -c {내 GitHub 아이디}             # 2. 처음 한 번: 내 브랜치 만들기 (이후엔 git switch 만)
git merge main                              # 3. 작업 시작 전 main 최신 내용 가져오기
git push -u origin {내 GitHub 아이디}        # 4. 작업 후 푸시 → main 으로 PR
```
- **브랜치는 사람당 하나**예요. 작업마다 새로 만들지 않고, main에 합친 뒤에도 같은 브랜치에서 이어서 해요.
- main으로 PR을 올리면 **자동으로 승인·머지**돼요 ([`.github/workflows/auto-merge.yml`](.github/workflows/auto-merge.yml)). 아직 합치면 안 되는 PR은 **Draft**로 올려요.
- 통합 전 개인 작업은 `backup/noviz-domino` 등 `backup/` 브랜치에 보관해요.

### 팀 규칙
1. **원본 데이터(`01_data/`, `02_questions/`, `03_answers/`)는 수정하지 않아요.**
2. **질문·기준 답안은 검색 색인에 넣지 않아요.** 넣으면 평가가 의미 없어져요.
3. **세트마다 검색 범위를 지켜요.** 기본 negative의 "자료에 없음"은 용어집+약관만 검색할 때 기준이에요.
4. **골든셋은 설정을 다 정한 뒤 한 번만** 평가해요.
5. **API 키는 `.env`에만** 두고 커밋하지 않아요.

---

## 9. 문서
| 문서 | 내용 |
|---|---|
| [기획서](docs/기획서.md) | 설계 전체 (구현의 기준) |
| [조율 결과](docs/plans/조율_결과.md) | 세 사람 구현을 비교해 기능별로 고른 근거 |
| [과제 안내](docs/과제_안내.md) | 과제 원문과 제출물 |
| [구현 결정 체크리스트](docs/구현_결정_체크리스트.md) | 단계별 결정 항목과 보기 |
| [Agent 구조도](docs/agent_graph.md) | LangGraph 노드·분기 (코드에서 자동 생성) |
