"""그래프에서 Mermaid 그림을 자동으로 뽑아 docs/agent_graph.md 로 저장 (제출물).  python -m src.agent.export"""
from __future__ import annotations

from pathlib import Path

from src.agent.graph import build_graph, export_mermaid
from src.common.config import ROOT, load_config

DOC = """# Agent 그래프 구조도

> 이 파일은 `python -m src.agent.export` 가 LangGraph 그래프에서 자동으로 만들어요. 직접 고치지 마세요.
> 구조도(기획서 4장)와 노드·갈래가 1:1 이에요: `tests/test_agent.py` 가 같은지 확인해요.

```mermaid
{mermaid}
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
| grade → rewrite → retrieve | 부족 & 재검색 < {retries} |
| grade → abstain | 부족 & 재검색 = {retries} |
| verify → END | 통과 |
| verify → generate | 실패 & 재생성 < {regen} (실패 이유를 같이 전달) |
| verify → abstain | 실패 & 재생성 = {regen} |
| grade / rewrite / generate → abstain | AI 호출 {cap}회 상한 도달 (route 1 + grade 3 + rewrite 2 + generate 2 = 8) |
"""


def main():
    cfg = load_config("configs/baseline.json")
    g = build_graph(None, None, cfg)
    a = cfg["agent"]
    text = DOC.format(mermaid=export_mermaid(g).strip(), retries=a["max_retrieval_retries"],
                      regen=a["max_regenerate"], cap=a["max_llm_calls"])
    out = ROOT / "docs" / "agent_graph.md"
    out.write_text(text, encoding="utf-8")
    print(f"저장: {out}")


if __name__ == "__main__":
    main()
