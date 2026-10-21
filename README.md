# financial-rag-agent

금융 문서(약관·용어집·금융생활 가이드북·정책 자료) PDF를 검색해 **근거와 함께 답하는 RAG Agent**를 만들고, 직접 만든 평가 지표와 평가 루프로 개선하는 팀 프로젝트예요.

과제 원문은 [docs/과제_안내.md](docs/과제_안내.md)에 그대로 있어요.

## 현재 상태
- 원본 데이터와 과제 안내, [구현 결정 체크리스트](docs/구현_결정_체크리스트.md)가 들어 있어요.
- [기획서](docs/기획서.md)의 8장 1~6단계(데이터 형식·준비 파이프라인·검색·기본 RAG+평가 루프·실험 설정·Agent)가 구현돼 있어요. 실행 방법은 아래 **실행 방법**에 있어요.
- 골든셋 작성(7단계)과 실제 Gemini 실행·실험 결과는 아직이에요.

## 진행 방식: 각자 계획 → AI 조율 → 회의 결정 → 구현
```
① 각자 작성   : 체크리스트 양식대로 자기 구현·계획을 docs/plans/{GitHub아이디}.md 에 작성
② PR로 올림   : 브랜치 plan/{GitHub아이디} → PR (파일이 서로 달라 충돌 없음)
③ AI 조율     : 세 문서 + 결과 파일을 별도의 Claude(Opus 5.5, 노력 수준 높게)에게 전달
                → 단계별 비교표, 공통점, 쟁점, 쟁점 해결용 실험 제안, 합의안 초안
④ 30분 회의   : 쟁점만 사람이 결정 → 아래 "결정 사항" 표에 기록
⑤ 구현        : 결정대로 공용 구조에서 담당을 나눠 구현 (할 일별 브랜치 → PR)
```

### 작성 규칙 (① 단계)
| 규칙 | 이유 |
|---|---|
| **실제로 만든 것·시험한 것** 기준으로 쓰고, 안 해 본 건 **"미검증"** 표시 | AI에게 "채워 줘"만 하면 세 문서가 비슷한 기본 추천으로 채워져서 비교 의미가 없어요 |
| 항목마다 **근거 종류**를 적기: `측정` / `시험해 봄` / `문서·의견` | 조율할 때 측정 수치와 의견이 같은 무게로 섞이지 않게 |
| 수치는 **같은 질문 세트·같은 채점 방법**으로 잰 것만 | 기준이 다르면 "0.8 vs 0.7"이 무의미해요 |
| 코드·결과 파일 **위치(경로나 링크)** 를 같이 적기 | 조율하는 쪽이 글만 보고 판단하지 않게 |

### 계획 문서 올리는 방법 (Claude Code 사용)
1. 이 저장소를 내 컴퓨터에 받아 두고, 그 폴더에서 Claude Code 를 열어요.
2. 아래 **요청문 ①**의 `{내 GitHub 아이디}`, `{Reviewer 아이디}`, 코드 위치를 바꿔서 보내요.
   → Claude Code 가 문서 작성 → 브랜치 `plan/{아이디}` 생성 → 커밋 → 푸시 → PR 생성까지 해요.
3. **Reviewer 는 돌아가며 지정**해요: noviz-domino → jjh7757 → yellowgreenbean → noviz-domino
4. 리뷰어는 PR 의 **Files changed** 에서 내용을 보고 **Approve** → **Merge** 해요.
   (리뷰도 Claude Code 에게 "PR #번호 내용 요약하고 빠진 항목 알려줘"라고 맡길 수 있어요)
5. 세 문서가 모두 main 에 합쳐지면 **요청문 ②**로 조율을 시작해요.

### 문서 형식 (docs/plans/{GitHub아이디}.md)
체크리스트의 단계 번호(0~12)를 그대로 쓰고, 단계마다 아래 표를 채워요.

| 항목 | 내 선택 | 근거 종류 | 수치·결과 | 장점 | 걱정되는 점 | 다른 안도 괜찮은가 |
|---|---|---|---|---|---|---|
| 1-1 라이브러리 | 예) pdfplumber | 측정 | 예) BM25 Hit@5 0.85 (가이드북+추가 80문항) | | | 예) pymupdf4llm 도 가능 |

### 요청문 ① 각자 Claude에게 (작성용)
```
financial-rag-agent 저장소의 docs/구현_결정_체크리스트.md 단계(0~12) 순서대로
내 구현·계획을 docs/plans/{내 GitHub 아이디}.md 에 정리해줘. 모든 답변은 한국어로.
- 내가 실제로 만든 코드·실험 결과를 기준으로 채우고, 안 해 본 건 "미검증"으로 표시해
- 항목마다 근거 종류(측정 / 시험해 봄 / 문서·의견)를 적어
- 수치는 어떤 질문 세트·어떤 채점 방법으로 잰 것인지 같이 적어
- 코드·결과 파일 위치를 적어
- README 의 "문서 형식" 표 양식을 따라줘
- 다 쓰면: main 을 최신으로 받고 → 브랜치 plan/{내 GitHub 아이디} 를 만들어서 커밋 → 푸시
  → PR 을 만들고 Reviewer 로 팀원 한 명({Reviewer 아이디})을 지정해줘
내 코드 위치: (여기에 경로)
```

### 요청문 ② 조율 Claude에게 (Opus 5.5, 노력 수준 높게)
```
financial-rag-agent 저장소 docs/plans/ 의 세 팀원 계획을 단계별(0~12)로 비교해줘. 모든 답변은 한국어로.
- 단계마다: 각자 선택, 근거 종류, 수치(같은 기준으로 잰 것만), 장단점을 표로
- "공통으로 합의된 것"과 "쟁점"을 나눠줘
- 쟁점마다 "어떤 실험 하나로 결론 낼 수 있는지" 제안해줘 (질문 세트·지표·고정 조건 포함)
- 근거가 '문서·의견'뿐인 선택은 따로 표시해줘
- 최종 결정은 하지 말고, 합의안 초안과 회의에서 물어볼 질문 목록으로 정리해서
  docs/plans/조율_결과.md 에 써줘
```

## 실행 방법
구현은 [docs/기획서.md](docs/기획서.md)를 그대로 따라요. 모든 명령은 **Git Bash**에서 저장소 루트에서 실행해요.

### 0. 준비
```bash
python -m venv .venv
source .venv/Scripts/activate            # macOS/Linux 는 source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env                     # .env 에 GEMINI_API_KEY 를 넣어요 (API 키 없이도 아래 "키 없이" 명령은 돌아가요)
pytest                                   # 단위·엔드투엔드 테스트 (네트워크·API 를 쓰지 않아요)
```

### 1. 준비 파이프라인 (PDF → 쪽 레코드 → 표·그림 → 청크 → 색인)
```bash
python -m src.pipeline.build --config configs/baseline.json                 # 전체 (Gemini 그림·표 설명 + 임베딩, 키 필요)
python -m src.pipeline.build --config configs/baseline.json --no-vlm        # 그림·표 Gemini 생략 (임베딩은 키 필요)
python -m src.pipeline.build --config configs/baseline.json --no-vlm --no-index   # 키 없이 청크까지
python -m src.pipeline.build --config configs/baseline.json --offline       # 가짜 AI·가짜 임베딩으로 끝까지 (키 불필요)
python -m src.pipeline.build --config configs/baseline.json --no-vlm --no-index --variants all   # 청킹 실험용 3종 모두
python -m src.sources.check                                                 # 정답 근거 ID 누락 확인 (0 이어야 해요)
```
산출물은 `data/`(git 제외): `pages.jsonl`, `sources.jsonl`, `figures.jsonl`, `figure_review.jsonl`(사람이 볼 그림), `chunks/*.jsonl`, `index/`(Chroma), `cache/`(Gemini·임베딩·형태소 캐시).
캐시를 지우지 않으면 다시 돌려도 Gemini 비용이 들지 않아요.

### 2. 평가 루프
```bash
# 키 없이: 가짜 AI 로 형식 확인 (결과는 runs/dry_{실험ID}/)
python -m src.evaluation.run --config configs/baseline.json --sets positive,negative --dry-run --limit 5
python -m src.evaluation.run --config configs/exp3_agent.json --sets positive,negative --dry-run --limit 5
# 키 없이: 검색 지표만 (BM25 면 API 불필요)
python -m src.evaluation.run --config configs/exp2b_bm25.json --sets positive,negative,life_guide,additional --retrieval-only
# 키가 있을 때: 180문항 실제 실행 (질문마다 AI 약 2회 + 채점 1회)
python -m src.evaluation.run --config configs/baseline.json --sets positive,negative,life_guide,additional
```
`runs/{실험ID}/`에 `config.json`(설정+기준 대비 diff+모델·프롬프트), `results.jsonl`, `scores.jsonl`, `summary.json`이 저장되고 `runs/experiments.jsonl`에 한 줄이 추가돼요. 같은 실험ID 폴더가 있으면 `--force` 없이는 멈춰요.

### 3. 실험 (기준 대비 한 가지씩 바꾼 설정)
| 실험 | 설정 파일 | 바꾸는 것 |
|---|---|---|
| 기준 | `configs/baseline.json` | 구조형 청킹 · 하이브리드 k=5 · 기본 RAG |
| 0 흔들림 | `configs/exp0_repeat.json` | 기준을 한 번 더 |
| 1 청킹 | `exp1a_recursive_500_50.json` / `exp1b_parentchild.json` | 글자 수 자르기 / parent 펼치기 |
| 2 검색 | `exp2a_vector.json` / `exp2b_bm25.json` | 뜻 검색만 / 단어 검색만 |
| 3 RAG vs Agent | `exp3_agent.json` | 흐름만 Agent 그래프로 |
| 4 제목 붙이기 | `exp4_embed_prefix.json` | 임베딩 대상 `text` → `embed_text` |
실험 설정 파일에는 바꾸는 항목만 적어요(나머지는 baseline). 청킹이 다른 실험은 먼저 `--variants all` 로 청크를 만들어 두세요 (없으면 평가 때 자동으로 만들어요).
골든셋은 `05_golden/golden_set.jsonl`에 사람이 쓰고(형식: `05_golden/golden_schema.json`), 설정을 확정한 뒤 `--sets golden` 으로 한 번만 평가해요.

### 4. Agent 구조도
```bash
python -m src.agent.export               # LangGraph → Mermaid, docs/agent_graph.md 갱신
```

## 폴더 구조
```
financial-rag-agent/
├─ 01_data/        원본 PDF 23개            (수정 금지)
├─ 02_questions/   질문 JSON 180문항        (수정 금지)
├─ 03_answers/     기준 답안 MD·JSONL       (수정 금지)
├─ src/            loading · tables · figures · sources · chunking · index · retrieval · generation · agent · evaluation · common · pipeline
├─ configs/        baseline.json, behavior_map.json, 실험 설정 (exp*.json)
├─ tests/          pytest (API 를 쓰지 않음)
├─ 05_golden/      골든셋 형식 (내용은 사람이 작성)
├─ data/ runs/     생성 결과 (git 제외)
└─ docs/
   ├─ 기획서.md                 구현 기준 문서
   ├─ agent_graph.md            Agent 구조도 (자동 생성)
   ├─ 과제_안내.md              과제 설명 원문
   ├─ 구현_결정_체크리스트.md    단계별(0~12) 정할 것·보기·예시
   └─ plans/                    팀원별 계획 문서와 조율 결과 (진행 방식 참고)
```

## 데이터 구성
| 세트 | 문항 | 질문 파일 | 검색 대상 |
|---|---|---|---|
| 기본 positive | POS001~060 (60) | `positive_questions.json` | 용어집 + 표준약관 |
| 기본 negative | NEG001~040 (40) | `negative_questions.json` | 용어집 + 표준약관 |
| 금융생활 positive | POS061~080 (20) | `positive_life_guide_questions.json` | 금융생활 가이드북 |
| 추가 문서 positive | ADD-* (60) | 문서별 `*_questions.json` | 추가 PDF 6개 |

| `01_data/` 경로 | 개수 | 내용 |
|---|---|---|
| `financial_glossary_readable.pdf` | 1 | 금융용어 정의 |
| `standard_terms/` | 15 | 증권 매매·신용거래·연금저축·전자금융·해외투자 등 표준약관 |
| `financial_life_guide.pdf` | 1 | 재무관리·저축·투자 가이드 (표·그래프 포함) |
| `additional/` | 6 | 주택연금, 퇴직연금, 주택 정책금융, 자영업자 채무, 보험약관 예시, 정책서민금융·채무조정 |

## 제출물 (과제 요구사항)
- RAG 실험 계획: 목적·가설, 사용할 PDF·질문 세트, 비교 설정과 고정 조건, 평가 지표, 결과 기록 형식
- Agent 그래프 구조도: 노드 역할, 흐름과 분기, 종료 조건
- RAG Agent와 평가 루프 코드, 실행 방법
- 사용한 PDF·질문 세트와 직접 작성·검수한 골든셋 (기준 답변·근거 포함)
- 실행 기록, 비교 실험 결과, 실패 사례와 개선점

## 팀 규칙
1. **원본 데이터(`01_data/`, `02_questions/`, `03_answers/`)는 수정하지 않아요.** 가공한 결과는 별도 폴더에 저장해요.
2. **질문·기준 답안은 검색 DB(인덱스)에 넣지 않아요.** 넣으면 평가가 의미 없어져요 (과제 조건).
3. **기본 negative 세트의 "자료에 없음"은 용어집 + 표준약관만 검색할 때 기준이에요.** 세트마다 검색 범위를 구분해요.
4. **직접 만든 골든셋은 제공 세트와 따로 평가해요.** 제공 세트로 설정을 고르고, 골든셋은 최종 확인용으로 써요.
5. **API 키는 `.env`에만** 두고 올리지 않아요 (`.gitignore`에 등록돼 있어요).

## 결정 사항 (팀 논의 후 채우기)
| 항목 | 결정 | 이유 | 날짜 |
|---|---|---|---|
| LLM / 임베딩 모델 | | | |
| PDF 로딩·표·그림 처리 방식 | | | |
| 청킹 전략 (기준 설정) | | | |
| 벡터 DB / 검색 방식 | | | |
| Agent 구조 | | | |
| 평가 지표 | | | |
| 결과 저장 형식 | | | |
| 브랜치·코드 리뷰 방식 | | | |
