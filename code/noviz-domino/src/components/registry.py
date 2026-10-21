"""교체 가능한 구성요소(OCR, VLM, PDF 텍스트 추출 …)를 이름으로 등록·선택하는 공통 레지스트리.

원칙
  1) 모든 구현은 같은 입력·출력 형식(각 모듈의 dataclass)을 지킨다 → 호출하는 쪽 코드는 그대로
  2) 라이브러리 import 는 생성 시점까지 미룬다 → 설치 안 된 엔진이 있어도 다른 엔진은 동작
  3) available() 이 '왜 못 쓰는지'(미설치, 보안 정책 차단, 키 없음)를 알려 준다

사용
  from src.components import ocr
  print(ocr.REGISTRY.available())          # {'easyocr': (True, 'ok'), 'rapidocr': (False, 'DLL 차단 …')}
  engine = ocr.REGISTRY.create("easyocr")
  lines = engine(image)                    # list[OCRLine]

새 구현 추가
  @REGISTRY.register("myocr", requires=["mylib"], note="설명")
  def make_myocr(**kw): ...  → 호출 가능한 객체 반환
"""
import importlib
import os
from dataclasses import dataclass, field


@dataclass
class Entry:
    name: str
    factory: callable
    requires: list = field(default_factory=list)     # import 가능해야 하는 모듈
    env: list = field(default_factory=list)          # 하나라도 있어야 하는 환경 변수 (API 키 등)
    note: str = ""


class Registry:
    def __init__(self, kind: str):
        self.kind = kind
        self._entries: dict[str, Entry] = {}

    def register(self, name, requires=(), env=(), note=""):
        def deco(factory):
            self._entries[name] = Entry(name, factory, list(requires), list(env), note)
            return factory
        return deco

    def names(self):
        return list(self._entries)

    def check(self, name):
        e = self._entries.get(name)
        if e is None:
            return False, f"등록되지 않은 {self.kind}: {name} (가능: {self.names()})"
        for mod in e.requires:
            try:
                importlib.import_module(mod)
            except ImportError as ex:
                msg = str(ex)
                if "정책" in msg or "policy" in msg.lower():
                    return False, f"보안 정책이 DLL 차단 ({mod})"
                return False, f"미설치/로드 실패: {mod} ({msg[:80]})"
            except OSError as ex:
                return False, f"로드 실패: {mod} ({str(ex)[:80]})"
        if e.env and not any(os.getenv(k) for k in e.env):
            return False, f"환경 변수 없음: {' 또는 '.join(e.env)}"
        return True, "ok"

    def available(self):
        return {n: self.check(n) for n in self._entries}

    def create(self, name, **kwargs):
        ok, why = self.check(name)
        if not ok:
            raise RuntimeError(f"{self.kind} '{name}' 사용 불가: {why}")
        return self._entries[name].factory(**kwargs)

    def first_available(self, preferred=()):
        for n in list(preferred) + self.names():
            if self.check(n)[0]:
                return n
        return None

    def report(self):
        lines = [f"[{self.kind}]"]
        for n, (ok, why) in self.available().items():
            lines.append(f"  {'✅' if ok else '❌'} {n:12s} {why if not ok else self._entries[n].note}")
        return "\n".join(lines)
