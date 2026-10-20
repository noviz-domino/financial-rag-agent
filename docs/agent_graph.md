# Agent 그래프 구조도

> 이 파일은 `python -m src.agent.export` 가 LangGraph 그래프에서 자동으로 만들어요. 직접 고치지 마세요.
> 구조도(기획서 4장)와 노드·갈래가 1:1 이에요: `tests/test_agent.py` 가 같은지 확인해요.

```mermaid
---
config:
  flowchart:
    curve: linear
---
graph TD;
	__start__([<p>__start__</p>]):::first
	route(route)
	retrieve(retrieve)
	grade(grade)
	rewrite(rewrite)
	expand(expand)
	generate(generate)
	verify(verify)
	refuse(refuse)
	clarify(clarify)
	defer_freshness(defer_freshness)
	abstain(abstain)
	__end__([<p>__end__</p>]):::last
	__start__ --> route;
	expand --> generate;
	generate -. &nbsp;cap&nbsp; .-> abstain;
	generate -. &nbsp;ok&nbsp; .-> verify;
	grade -. &nbsp;exhausted_or_cap&nbsp; .-> abstain;
	grade -. &nbsp;link&nbsp; .-> expand;
	grade -. &nbsp;sufficient&nbsp; .-> generate;
	grade -. &nbsp;retry&nbsp; .-> rewrite;
	retrieve --> grade;
	rewrite -. &nbsp;cap&nbsp; .-> abstain;
	rewrite -. &nbsp;ok&nbsp; .-> retrieve;
	route -.-> clarify;
	route -.-> defer_freshness;
	route -.-> refuse;
	route -.-> retrieve;
	verify -. &nbsp;pass&nbsp; .-> __end__;
	verify -. &nbsp;fail&nbsp; .-> abstain;
	verify -. &nbsp;regen&nbsp; .-> generate;
	abstain --> __end__;
	clarify --> __end__;
	defer_freshness --> __end__;
	refuse --> __end__;
	classDef default fill:#f2f0ff,line-height:1.2
	classDef first fill-opacity:0
	classDef last fill:#bfb6fc
```

## 노드 역할
| 노드 | 하는 일 | 방법 |
|---|---|---|
| route | 질문 유형·안전성 판단, 인젝션이면 지시를 뺀 `clean_query` 생성 | AI 1회 |
| retrieve | `search(query, k, scope)` (하이브리드, 범위 강제) | 검색 도구 |
| grade | 근거가 충분한지, 링크로 보완 가능한지, 무엇이 부족한지 판단 | AI 1회 |
| rewrite | 부족한 정보를 바탕으로 검색어 다시 쓰기 | AI 1회 |
| expand | `get_parent`·`follow_ref` 로 링크를 따라 근거 보충 (2차, `agent.expand` 설정이 꺼져 있으면 분기 없음) | 검색 도구 |
| generate | 답변 JSON 작성 (재생성이면 verify 실패 이유를 같이 전달) | AI 1회 |
| verify | 인용이 검색 결과에 있는지, 숫자가 근거에 있는지 | **규칙만, AI 호출 없음** |
| refuse / clarify / defer_freshness | route 판단에 따른 정형 응답 | AI 없음 |
| abstain | 근거 부족을 밝히고 답하지 않음 | AI 없음 |

## 분기와 종료 조건
| 분기 | 조건 |
|---|---|
| route → refuse / clarify / defer_freshness | route 가 해당 대응 행동으로 판단 |
| grade → generate | 근거 충분 |
| grade → expand → generate | 링크로 보완 가능 (2차, 설정 `agent.expand`) |
| grade → rewrite → retrieve | 부족 & 재검색 < 2 |
| grade → abstain | 부족 & 재검색 = 2 |
| verify → END | 통과 |
| verify → generate | 실패 & 재생성 < 1 (실패 이유를 같이 전달) |
| verify → abstain | 실패 & 재생성 = 1 |
| grade / rewrite / generate → abstain | AI 호출 8회 상한 도달 (route 1 + grade 3 + rewrite 2 + generate 2 = 8) |
