import json, time
from typing import Any
from .config import settings

try:
    from hindsight_client import Hindsight
except ImportError:
    Hindsight = None

class MemoryService:
    def __init__(self):
        self.client = None
        self.error = "Hindsight client is not installed"
        if Hindsight:
            try:
                self.client = Hindsight(base_url=settings.hindsight_base_url, api_key=settings.hindsight_api_key or None, timeout=20)
                self.error = ""
            except Exception as exc: self.error = str(exc)

    def ensure_bank(self):
        if not self.client: raise RuntimeError(self.error or "Hindsight is unavailable")
        try:
            return self.client.create_bank(bank_id=settings.hindsight_bank_id, name="IncidentMind", mission="Remember production incidents, root causes, troubleshooting steps, fixes, runbooks and post-mortem lessons for an engineering organization.")
        except Exception as exc:
            # Existing banks may reject creation; confirm access via a harmless recall.
            try: self.client.recall(bank_id=settings.hindsight_bank_id, query="production incident memory bank")
            except Exception: raise exc

    def retain(self, content: str, metadata: dict[str, Any], context: str = "IncidentMind engineering operations memory"):
        start = time.perf_counter()
        if not self.client: raise RuntimeError(self.error or "Hindsight is unavailable")
        tags=metadata.get("tags", []) or []
        safe_metadata={str(k):str(v) for k,v in metadata.items() if k!="tags" and v is not None}
        if tags: safe_metadata["tags"] = ", ".join(str(tag) for tag in tags)
        result = self.client.retain(bank_id=settings.hindsight_bank_id, content=content, context=context, metadata=safe_metadata, tags=[str(tag) for tag in tags])
        return result, round((time.perf_counter()-start)*1000)

    def recall(self, query: str):
        start = time.perf_counter()
        if not self.client: raise RuntimeError(self.error or "Hindsight is unavailable")
        result = self.client.recall(bank_id=settings.hindsight_bank_id, query=query, budget="mid")
        rows = [self._memory(m) for m in getattr(result, "results", [])]
        return rows, round((time.perf_counter()-start)*1000)

    def reflect(self, query: str):
        start = time.perf_counter()
        if not self.client: raise RuntimeError(self.error or "Hindsight is unavailable")
        result = self.client.reflect(bank_id=settings.hindsight_bank_id, query=query, budget="mid")
        return {"text": getattr(result, "text", str(result)), "based_on": [str(x) for x in (getattr(result, "based_on", []) or [])]}, round((time.perf_counter()-start)*1000)

    def list_memories(self, limit=100, offset=0):
        if not self.client: raise RuntimeError(self.error or "Hindsight is unavailable")
        result = self.client.list_memories(bank_id=settings.hindsight_bank_id, limit=limit, offset=offset)
        return [self._memory(m) for m in getattr(result, "items", [])], getattr(result, "total", 0)

    @staticmethod
    def _memory(m):
        def get(key, default=None):
            return m.get(key, default) if isinstance(m, dict) else getattr(m, key, default)
        scores=get("scores")
        if scores and not isinstance(scores, dict):
            scores={name:getattr(scores,name,None) for name in ("final","reranker","semantic","keyword")}
        return {"id": str(get("id", "")), "text": str(get("text", get("content", m))), "type": str(get("type", "memory")), "metadata": get("metadata", {}) or {}, "scores":scores or {}, "created_at": str(get("created_at", ""))}

memory = MemoryService()
