from time import perf_counter

from pydantic import ValidationError

from app.ai.ollama_client import AIError, OllamaClient, strict_json
from app.ai.prompts import PROMPT_VERSION, build_messages
from app.ai.schemas import ParseRequest, ParseResponse, RuleDraft, generation_schema
from app.ai.validation import check_rule, reject, schema_errors
from app.rules.validation import issue


def parse_rule(payload: ParseRequest, client: OllamaClient) -> ParseResponse:
    start = perf_counter()
    content = client.chat(build_messages(payload.text, payload.history_interval), generation_schema())
    try:
        raw = strict_json(content)
    except (ValueError, RecursionError) as exc:
        raise AIError("invalid_json", "模型沒有輸出完整且唯一的 JSON 物件，請重試或簡化條件。", 502) from exc
    if isinstance(raw, dict) and raw.get("rule") is not None:
        check_rule(raw["rule"])
    try:
        draft = RuleDraft.model_validate(raw)
    except ValidationError as exc:
        reject(schema_errors(exc))
    # Never expose a partial executable tree if any requested condition is unresolved.
    invalid_quotes = [issue("invalid_assumption", f"/assumptions/{i}/source_text", "假設引用文字不在原始輸入中。")
                      for i, item in enumerate(draft.assumptions) if item.source_text not in payload.text]
    if invalid_quotes:
        raise AIError("invalid_assumption", "模型假設未正確引用原始文字，請重試或改寫條件。", 422, errors=invalid_quotes)
    if draft.unresolved_conditions:
        draft.rule = None
    return ParseResponse(
        **draft.model_dump(), original_text=payload.text, history_interval=payload.history_interval,
        model=client.config.model, prompt_version=PROMPT_VERSION,
        status="needs_clarification" if draft.rule is None else "draft",
        elapsed_ms=round((perf_counter() - start) * 1000),
    )
