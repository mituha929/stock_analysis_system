import time
from datetime import datetime, timezone
from types import SimpleNamespace

from app.rules.context import build_context
from app.rules.engine import evaluate
from app.screening.ranking import rank_results
from app.services.mysql_instrument_repository import MySQLRepositoryError
from app.signals import analyze_technical


def prefilter_reasons(candle, config):
    reasons = []
    for field, value in (("price", candle.close), ("volume", candle.volume)):
        low, high = getattr(config, f"min_{field}"), getattr(config, f"max_{field}")
        if low is not None and value < low:
            reasons.append(f"{field} {value} < 最小值 {low}")
        if high is not None and value > high:
            reasons.append(f"{field} {value} > 最大值 {high}")
    return reasons


def screen(payload, repository, watchlist=None, clock=time.monotonic, *, quality_policy=None):
    started = clock()
    scope = payload.universe
    symbols = scope.symbols if scope.kind == "symbols" else list(dict.fromkeys(watchlist or [])) if scope.kind == "watchlist" else None
    instruments = repository.instruments(scope.markets, scope.category_nos, payload.candle_table, symbols)
    # Storage is keyed by symbol+interval; preserve all markets but analyze each symbol once.
    unique = {}
    for instrument in instruments:
        if instrument["symbol"] not in unique:
            unique[instrument["symbol"]] = {**instrument, "markets": []}
        unique[instrument["symbol"]]["markets"].append(instrument["market_no"])
    universe = list(unique.values())
    if len(universe) > 10000:
        raise ValueError("選股範圍超過 10000 檔，請縮小市場或分類範圍")
    omitted = sorted(set(symbols or []) - unique.keys())
    excluded = [{"symbol": symbol, "status": "outside_universe", "reasons": ["主檔不存在、已停用或不符市場／分類範圍"]} for symbol in omitted]
    matched, processed, analyzed = [], 0, 0
    counts = {"prefiltered": 0, "missing_data": 0, "insufficient_data": 0, "not_matched": 0, "analysis_error": 0}
    if quality_policy: counts.update(stale_data=0,unverified_period=0)
    for instrument in universe:
        if clock() - started >= payload.timeout_seconds:
            break
        processed += 1
        symbol = instrument["symbol"]
        def exclude(status, reasons, metadata=None):
            counts[status] += 1
            excluded.append({"symbol": symbol, "status": status, "reasons": reasons,**(metadata or {})})
        try:
            quality_info={}
            if quality_policy:
                candles,metadata=repository.confirmed_candles(symbol,payload.history_interval,payload.candle_table,payload.history_bars,quality_policy)
                problem,reasons,quality_info=quality_policy.assess(candles,metadata,symbol,instrument["markets"])
                if problem:
                    exclude(problem,reasons,quality_info)
                    continue
                latest=candles[-1:]
                written_at=metadata["latest_written_at"]
            else:
                latest, _ = repository.candles(symbol, payload.history_interval, payload.candle_table, 1)
            if not latest:
                exclude("missing_data", ["尚無所選週期 K 線"])
                continue
            reasons = prefilter_reasons(latest[-1], payload.prefilter)
            if reasons:
                exclude("prefiltered", reasons,quality_info)
                continue
            if not quality_policy:
                candles, written_at = repository.candles(symbol, payload.history_interval, payload.candle_table, payload.history_bars)
            if len(candles) < 2:
                exclude("insufficient_data", ["至少需要兩根 K 線"],quality_info)
                continue
            analyzed += 1
            result = analyze_technical(candles)
            current, previous = build_context(SimpleNamespace(candles=candles, indicators=result["indicators"], scores=result["scores"], summary=result["summary"]))
            decision = evaluate(payload.rule, current, previous)
            if quality_policy: quality_policy.describe(decision)
            if not decision["matched"]:
                exclude("insufficient_data" if decision["status"] == "insufficient_data" else "not_matched", [d["reason"] for d in decision["details"] if d["matched"] is not True],quality_info)
                continue
            matched.append({**instrument, **decision, "price": current["price"], "volume": current["volume"], "direction_score": current["direction_score"], "risk_score": current["risk_score"],
                            "candle_count": len(candles), "latest_candle_at": candles[-1].timestamp, "latest_written_at": written_at,
                            "warnings": [*result["warnings"], *(["未滿20根 K 線，評分不可用"] if len(candles) < 20 else [])]})
        except MySQLRepositoryError:
            # A broken connection is a failed scan, never a misleading empty success.
            raise
        except (ValueError, TypeError, ArithmeticError) as exc:
            exclude("analysis_error", [str(exc)])
    ranked = rank_results(matched, payload.sort)
    complete = processed == len(universe)
    response = {"status": "complete" if complete else "partial", "complete": complete, "ranking_complete": complete,
            "source": "mysql", "history_interval": payload.history_interval, "candle_table": payload.candle_table, "history_bars": payload.history_bars,
            "generated_at": datetime.now(timezone.utc), "elapsed_seconds": round(clock() - started, 3),
            "universe_count": len(universe), "processed_count": processed, "analyzed_count": analyzed, "matched_count": len(ranked),
            "returned_count": min(len(ranked), payload.limit), "truncated": len(ranked) > payload.limit,
            "remaining_symbols": [i["symbol"] for i in universe[processed:]], "excluded_counts": counts, "outside_universe_count": len(omitted),
            "excluded": excluded, "sort": [s.model_dump() for s in payload.sort], "items": ranked[:payload.limit],
            "warnings": ["以已儲存 K 線分析，未驗證是否收盤或資料新鮮度；各股資料日期可能不同。", "history_bars 會影響 EMA／評分，跨 API 比較時應使用相同 K 線範圍。", *([] if complete else ["已達時間預算，結果僅為已處理股票的排序；請縮小範圍或增加 timeout_seconds。"])]}
    if quality_policy:
        response["data_quality"]=quality_policy.snapshot()
        response["warnings"][0]="已排除過期或未確認完整期間資料；掃描完成不代表每檔資料都可用，分數不是勝率。"
    return response
