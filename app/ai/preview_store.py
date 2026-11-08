"""Bounded process-local, expiring, single-use previews. No chat/database writes."""
from copy import deepcopy
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
import hashlib
import json
import secrets
from threading import Lock
from time import monotonic
from app.ai.ollama_client import AIError


class PreviewStore:
    def __init__(self, ttl=600, capacity=64, clock=monotonic):
        self.ttl,self.capacity,self.clock=ttl,capacity,clock
        self.entries=OrderedDict(); self.lock=Lock()

    def _prune(self):
        now=self.clock()
        for key in [k for k,v in self.entries.items() if v["deadline"]<=now]:
            del self.entries[key]
        while len(self.entries)>self.capacity:
            self.entries.popitem(last=False)

    def issue(self, data):
        serialized=json.dumps(data,sort_keys=True,ensure_ascii=False,separators=(",",":"),allow_nan=False)
        revision=hashlib.sha256(serialized.encode("utf-8")).hexdigest()
        key=secrets.token_urlsafe(32)
        with self.lock:
            self._prune()
            self.entries[key]=dict(data=deepcopy(data),revision=revision,deadline=self.clock()+self.ttl,status="ready")
            self._prune()
        return dict(preview_id=key,revision=revision,
            expires_at=(datetime.now(timezone.utc)+timedelta(seconds=self.ttl)).isoformat())

    def invalidate(self, key):
        with self.lock:
            self.entries.pop(key,None)

    def revoke_for_edit(self,key):
        with self.lock:
            self._prune()
            entry=self.entries.get(key)
            if entry is not None and entry["status"]=="ready": entry["status"]="editing"

    def read(self, key, revision, *, claim=False, for_revision=False):
        with self.lock:
            self._prune()
            entry=self.entries.get(key)
            if entry is None:
                raise AIError("preview_expired","預覽已失效、過期或服務已重新啟動，請重新解析及確認。",409)
            if not secrets.compare_digest(entry["revision"],revision):
                raise AIError("preview_changed","預覽版本不符，請重新確認最新條件。",409)
            if entry["status"] not in ({"ready","editing"} if for_revision else {"ready"}):
                raise AIError("preview_used","此預覽已執行或正在執行，請重新預覽；不重複送出。",409)
            if claim: entry["status"]="used"
            return deepcopy(entry["data"])


preview_store=PreviewStore()
