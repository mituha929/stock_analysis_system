import json
import os
import re
import socket
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


class AIError(Exception):
    def __init__(self, code: str, message: str, status: int, *, errors: list[dict] | None = None):
        super().__init__(message)
        self.code, self.status = code, status
        self.errors = errors

    def detail(self):
        result = {"code": self.code, "message": str(self)}
        if self.errors is not None:
            result["errors"] = self.errors
        return result


def strict_json(text: str):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    def constant(value):
        raise ValueError("non-finite JSON number")

    return json.loads(text, object_pairs_hook=pairs, parse_constant=constant)


@dataclass(frozen=True)
class OllamaConfig:
    enabled: bool = True
    base_url: str = "http://127.0.0.1:11434"
    model: str = "qwen3:4b-instruct"
    timeout: int = 60
    keep_alive: str = "0"
    num_ctx: int = 4096

    @classmethod
    def from_env(cls):
        try:
            enabled = os.getenv("OLLAMA_ENABLED", "true").lower().strip()
            if enabled not in ("true", "false", "1", "0"):
                raise ValueError("invalid enabled flag")
            config = cls(
                enabled=enabled in ("true", "1"),
                base_url=os.getenv("OLLAMA_BASE_URL", cls.base_url).rstrip("/"),
                model=os.getenv("OLLAMA_RULE_MODEL", cls.model).strip(),
                timeout=int(os.getenv("OLLAMA_RULE_TIMEOUT_SECONDS", "60")),
                keep_alive=os.getenv("OLLAMA_RULE_KEEP_ALIVE", cls.keep_alive),
                num_ctx=int(os.getenv("OLLAMA_RULE_NUM_CTX", "4096")),
            )
            url = urlsplit(config.base_url)
            if (url.scheme not in ("http", "https") or not url.hostname or url.username
                    or url.password or url.query or url.fragment or url.path):
                raise ValueError("base URL must be an HTTP origin")
            _ = url.port
            if (not config.model or len(config.model) > 200 or not 1 <= config.timeout <= 300
                    or not 4096 <= config.num_ctx <= 32768
                    or not re.fullmatch(r"(?:0|[1-9][0-9]*[smh])", config.keep_alive)):
                raise ValueError("invalid model settings")
            return config
        except ValueError as exc:
            raise AIError("invalid_config", "Ollama 設定無效，請檢查 OLLAMA_* 環境變數。", 503) from exc


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class OllamaClient:
    def __init__(self, config: OllamaConfig):
        self.config = config

    def chat(self, messages: list[dict], schema: dict) -> str:
        if not self.config.enabled:
            raise AIError("ai_disabled", "自然語言解析已停用。", 503)
        body = {
            "model": self.config.model, "messages": messages, "format": schema,
            "think": False, "stream": False, "keep_alive": 0 if self.config.keep_alive == "0" else self.config.keep_alive,
            "options": {"temperature": 0, "seed": 42, "num_ctx": self.config.num_ctx, "num_predict": 700},
        }
        request = Request(
            self.config.base_url + "/api/chat",
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json; charset=utf-8"}, method="POST",
        )
        try:
            # A local prompt must not be forwarded to an OS-configured HTTP proxy.
            with build_opener(ProxyHandler({}), NoRedirect()).open(request, timeout=self.config.timeout) as response:
                raw = response.read(262145)
            if len(raw) > 262144:
                raise AIError("response_too_large", "模型回應超過大小限制。", 502)
            envelope = strict_json(raw.decode("utf-8"))
            if not isinstance(envelope, dict):
                raise ValueError("invalid envelope")
            if envelope.get("done") is not True or envelope.get("done_reason") == "length":
                raise AIError("incomplete_response", "模型輸出未完成，請縮短或拆分條件。", 502)
            message = envelope.get("message")
            if not isinstance(message, dict) or message.get("tool_calls"):
                raise ValueError("invalid message")
            content = message.get("content")
            if not isinstance(content, str) or not content.strip():
                raise ValueError("empty content")
            return content
        except HTTPError as exc:
            if exc.code == 404:
                raise AIError("model_not_found", "Ollama 找不到指定模型或端點，請確認 OLLAMA_RULE_MODEL 與已安裝模型。", 503) from exc
            raise AIError("ollama_http_error", "Ollama 回傳 HTTP 錯誤，請查看本機 Ollama 狀態。", 502) from exc
        except (TimeoutError, socket.timeout) as exc:
            raise AIError("ollama_timeout", "模型解析逾時，請稍後重試或縮短條件。", 504) from exc
        except URLError as exc:
            if isinstance(exc.reason, (TimeoutError, socket.timeout)):
                raise AIError("ollama_timeout", "模型解析逾時。", 504) from exc
            raise AIError("ollama_unavailable", "無法連線 Ollama，請確認服務與 OLLAMA_BASE_URL。", 503) from exc
        except (ValueError, UnicodeError, RecursionError) as exc:
            raise AIError("invalid_response", "Ollama 回應格式不完整或不是有效 JSON。", 502) from exc
        except OSError as exc:
            raise AIError("ollama_unavailable", "讀取 Ollama 回應失敗。", 503) from exc
