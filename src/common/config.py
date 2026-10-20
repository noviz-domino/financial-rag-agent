"""설정 로딩. 입력은 configs/*.json, 기준(baseline.json)과 다른 항목은 자동 diff."""
from __future__ import annotations

import copy
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = ROOT / "configs"
BASELINE_PATH = CONFIG_DIR / "baseline.json"


class UnknownBehaviorLabel(KeyError):
    """behavior_map 에 없는 expected_behavior 값. 조용히 넘어가지 않고 멈춘다."""


def read_json(path) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def deep_merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def load_config(path) -> dict:
    """baseline 위에 path 의 값을 덮어쓴 전체 설정. (실험 설정 파일은 바꾸는 항목만 적으면 된다)"""
    base = read_json(BASELINE_PATH)
    p = Path(path)
    if p.resolve() == BASELINE_PATH.resolve():
        return base
    over = {k: v for k, v in read_json(p).items() if not k.startswith("_")}
    return deep_merge(base, over)


def diff_config(cfg: dict, base: dict | None = None, _prefix="") -> dict:
    """기준 대비 바뀐 항목: {"retrieval.mode": {"baseline": "hybrid", "value": "bm25"}}"""
    base = base if base is not None else read_json(BASELINE_PATH)
    out = {}
    for k in sorted(set(cfg) | set(base)):
        if k in ("description", "exp_id"):
            continue
        a, b = base.get(k), cfg.get(k)
        key = f"{_prefix}{k}"
        if isinstance(a, dict) and isinstance(b, dict):
            out.update(diff_config(b, a, key + "."))
        elif a != b:
            out[key] = {"baseline": a, "value": b}
    return out


def data_dir(cfg: dict) -> Path:
    p = Path(cfg["paths"]["data"])
    return p if p.is_absolute() else ROOT / p


def chunk_variant_name(cfg: dict) -> str:
    """청킹 설정 -> 파일 이름. 청킹을 바꾼 실험은 다른 청크 파일·인덱스를 쓴다."""
    c = cfg["chunking"]
    s = c["strategy"]
    if s == "structural":
        return "structural"
    if s == "recursive":
        return f"recursive_{c['size']}_{c['overlap']}"
    if s == "parentchild":
        return f"parentchild_{c['child_max']}"
    raise ValueError(f"알 수 없는 청킹 전략: {s}")


class BehaviorMap:
    """configs/behavior_map.json 래퍼. 22(+positive 1)종 -> 7가지 -> 5묶음."""

    def __init__(self, data: dict):
        self.map = dict(data["map"])
        self.groups5 = dict(data["groups5"])
        self.injection_labels = set(data.get("injection_labels", []))

    @classmethod
    def load(cls, path=None):
        return cls(read_json(path or CONFIG_DIR / "behavior_map.json"))

    def to7(self, label: str) -> str:
        if label in self.map:
            return self.map[label]
        if label in self.groups5:          # 골든셋은 7가지 값을 바로 쓸 수 있다
            return label
        raise UnknownBehaviorLabel(f"behavior_map.json 에 없는 expected_behavior: {label!r}")

    def to5(self, label7: str) -> str:
        if label7 not in self.groups5:
            raise UnknownBehaviorLabel(f"알 수 없는 대응 행동: {label7!r}")
        return self.groups5[label7]

    def is_injection(self, label: str) -> bool:
        return label in self.injection_labels
