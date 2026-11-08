from copy import deepcopy
from app.ai.ollama_client import AIError
from app.ai.preview_schemas import PreviewSettings
from app.ai.preview_store import preview_store
from app.ai.rule_preview import explain_rule, input_issues, rule_issues
from app.ai.validation import validated_rule
from app.services.screener_quality import ScreenerQuality

WARNINGS=["技術方向／風險分數不是勝率、報酬率或虧損機率，結果不是獲利保證。",
    "規則合法不代表 AI 理解一定正確；請逐條核對原文、假設及全部／任一分組。"]


def make_preview(text,settings,parsed=None,issues=(),watchlist=None,store=None):
    store=store or preview_store
    raw=parsed.model_dump(mode="json") if parsed is not None else {}
    rule=raw.get("rule")
    unresolved=list(dict.fromkeys([*issues,*raw.get("unresolved_conditions",[])]))
    if rule is not None:
        rule=validated_rule(rule).model_dump(mode="json")
        unresolved.extend(rule_issues(rule))
    if settings.universe.kind=="watchlist" and not watchlist:
        unresolved.append("目前自選股清單為空；不會自動擴大為全市場。")
    if unresolved: rule=None
    data=dict(original_text=text,settings=settings.model_dump(mode="json"),rule=rule,
        assumptions=raw.get("assumptions",[]),unresolved_conditions=list(dict.fromkeys(unresolved)),
        model=raw.get("model"),prompt_version=raw.get("prompt_version"),
        resolved_watchlist=list(watchlist or []) if settings.universe.kind=="watchlist" else None)
    return publish_preview(data,store)


def publish_preview(data,store):
    policy=ScreenerQuality(data["settings"]["history_interval"]).snapshot()
    data["expected_period_end"]=policy["expected_period_end"]
    result=deepcopy(data)
    result.update(status="draft" if data["rule"] else "needs_clarification",requires_confirmation=True,
        explanation=explain_rule(data["rule"],data["settings"]["history_interval"]) if data["rule"] else None,
        warnings=list(WARNINGS),data_policy=policy)
    if data["settings"]["candle_table"]=="odd_lot_candle":
        result["warnings"].append("零股商品範圍的歷史表不保證為純零股成交 K 線；本版沿用現有資料來源。")
    if data["rule"]: result.update(store.issue(data))
    else: result.update(preview_id=None,revision=None,expires_at=None)
    return result


def revise_preview(payload,store=None):
    store=store or preview_store
    existing=store.read(payload.preview_id,payload.revision,for_revision=True)
    if existing.get("intent") in {"single_stock_entry", "single_stock_exit", "single_stock_add", "single_stock_reduce"}:
        raise AIError("needs_clarification","此單股預覽使用固定範例策略；若需自訂門檻，請改寫完整技術條件後重新解析。",422)
    rule=validated_rule(payload.rule).model_dump(mode="json")
    problems=rule_issues(rule)
    if problems: raise AIError("needs_clarification","；".join(problems),422)
    data=store.read(payload.preview_id,payload.revision,claim=True,for_revision=True)
    store.invalidate(payload.preview_id)
    data["rule"]=rule
    for assumption in data["assumptions"]:
        assumption["interpreted_as"]="使用者已修改條件；請依最新白話預覽重新確認，不沿用原模型門檻。"
    if not data["assumptions"]:
        data["assumptions"]=[dict(source_text="使用者修改門檻",interpreted_as="原始文字保留作為對照；本次實際條件以修改後的白話預覽為準，請重新核對。")]
    return publish_preview(data,store)


def confirmed_data(payload,store=None):
    store=store or preview_store
    data=store.read(payload.preview_id,payload.revision)
    if payload.text!=data["original_text"] or payload.settings.model_dump(mode="json")!=data["settings"]:
        raise AIError("preview_changed","需求或執行設定已變更，請重新解析／預覽，不能執行舊版本。",409)
    settings=PreviewSettings.model_validate(data["settings"])
    rule=validated_rule(data["rule"]).model_dump(mode="json")
    problems=input_issues(data["original_text"],settings)+rule_issues(rule)
    if data["unresolved_conditions"] or problems:
        raise AIError("needs_clarification","條件尚未釐清，不允許執行部分規則。",422)
    config=settings.screener(rule)
    if ScreenerQuality(config.history_interval).end.isoformat()!=data["expected_period_end"]:
        raise AIError("preview_changed","最近已完成交易期間已改變，請重新預覽日期與條件再確認。",409)
    store.read(payload.preview_id,payload.revision,claim=True)
    return data,config
