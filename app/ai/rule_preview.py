"""Deterministic explanations and conservative clarification, not financial predictions."""
import re

PERIOD={"day":"日","week":"週","month":"月"}
NAMES={"price":"最新完整 K 線收盤價","volume":"成交量（原始資料單位）",
    "change_percent":"相較前一根 K 線的漲跌幅（%）","trend":"技術趨勢分類",
    "direction_score":"技術方向分數（不是上漲機率）","risk_score":"技術風險分數（不是虧損機率）",
    "trend_score":"趨勢分數","momentum_score":"動能分數","volume_score":"量價分數","structure_score":"結構分數",
    "macd_dif":"MACD DIF","macd_signal":"MACD 訊號線","macd_histogram":"MACD 柱狀體",
    "kd_k":"KD 的 K 值","kd_d":"KD 的 D 值","adx":"ADX 趨勢強度","atr14":"14根 K 線的 ATR 波動指標",
    "roc12":"12根 K 線的價格變動率（%）","bollinger_upper":"布林通道上軌",
    "bollinger_middle":"布林通道中軌","bollinger_lower":"布林通道下軌","obv":"OBV 量能指標（單位待確認）",
    "macd_golden_cross":"本根 K 線出現 MACD 黃金交叉","macd_death_cross":"本根 K 線出現 MACD 死亡交叉",
    "kd_golden_cross":"本根 K 線出現 KD 黃金交叉","kd_death_cross":"本根 K 線出現 KD 死亡交叉",
    "breakout_20d_high":"收盤價突破前20根 K 線高點","breakdown_20d_low":"收盤價跌破前20根 K 線低點"}
TRENDS={"strong_bullish":"強勢偏多","bullish":"偏多","neutral":"中性","bearish":"偏空","strong_bearish":"強勢偏空"}
OPS={">":"高於",">=":"不低於","<":"低於","<=":"不超過","==":"等於","!=":"不等於",
    "cross_above":"由前一根不高於，變成本根高於（向上穿越）",
    "cross_below":"由前一根不低於，變成本根低於（向下穿越）"}
UNITS_UNCONFIRMED={"volume","volume_ma5","volume_ma20","obv"}


def field_label(field, interval):
    unit=PERIOD[interval]
    if re.fullmatch(r"ma\d+",field): return f"最近{field[2:]}根{unit} K 線的平均收盤價（{field.upper()}）"
    if re.fullmatch(r"ema\d+",field): return f"最近{field[3:]}根{unit} K 線的指數移動平均（{field.upper()}）"
    if re.fullmatch(r"rsi\d+",field): return f"{field[3:]}根{unit} K 線的 RSI 強弱指標"
    if field.startswith("volume_ratio_"): return f"成交量相對最近{field.split('_')[-1][:-1]}根{unit} K 線平均量的倍數"
    if field.startswith("volume_ma"): return f"最近{field[9:]}根{unit} K 線的平均成交量（單位待確認）"
    return NAMES.get(field,field)


def explain_rule(raw, interval, path="/rule"):
    if "logic" in raw:
        return dict(kind="group",path=path,logic=raw["logic"],label="全部符合（AND）" if raw["logic"]=="AND" else "任一符合（OR）",
            children=[explain_rule(child,interval,f"{path}/rules/{i}") for i,child in enumerate(raw["rules"])])
    field,op,value=raw["field"],raw["operator"],raw["value"]
    if isinstance(value,dict): label=field_label(value["field"],interval)
    elif type(value) is bool: label="成立" if value else "不成立"
    elif isinstance(value,str): label=TRENDS.get(value,value)
    elif isinstance(value,list): label=f"{value[0]:g}～{value[1]:g}（含兩端）"
    else: label=f"{value:g}"
    return dict(kind="rule",path=path,field=field,operator=op,value=value,
        label=f"{field_label(field,interval)} {'介於' if op=='between' else OPS[op]} {label}")


def input_issues(text, settings):
    """Known unsupported intents fail closed; not a proof of all language semantics."""
    issues=[]
    if re.search(r"賺錢|穩賺|保證.*(?:獲利|上漲)|一定.*(?:漲|賺)|必漲|上漲機率|獲利機率",text):
        issues.append("無法事先確定哪檔會獲利；請改為明確的技術觀察條件。技術分數不能代表獲利機率。")
    if re.search(r"財報|營收|EPS|本益比|殖利率|新聞|籌碼|法人|公司.*獲利|本金|持股|成本|減碼|賣出一半",text,re.I):
        issues.append("目前只支援既有技術指標；財報、新聞、籌碼與個人持股損益不支援，不能用技術分數代替。")
    if re.search(r"再加上|把剛才|上一個條件|放寬一點|刪掉剛才",text):
        issues.append("本版不保留多輪策略上下文；請在輸入框寫出完整條件後重新解析。")
    requested={code for word,code in (("上市零股",5),("上櫃零股",6),("上市",0),("上櫃",1),("興櫃",4)) if word in text}
    if "上市零股" in text: requested.discard(0)
    if "上櫃零股" in text: requested.discard(1)
    if requested-set(settings.universe.markets):
        issues.append("文字提及的市場與範圍選項不一致；請先修改選項或需求，不會自動覆寫。")
    periods={p for pattern,p in ((r"日[線Kk]|日\s*K", "day"),(r"[週周][線Kk]|[週周]\s*K","week"),(r"月[線Kk]|月\s*K","month")) if re.search(pattern,text)}
    if periods-{settings.history_interval} or re.search(r"\d+\s*分[鐘Kk線]",text):
        issues.append("文字與所選 K 線週期不一致或混用週期；第一版只支援單一日／週／月 K。")
    if settings.universe.kind!="symbols" and re.search(r"(?:股票|代號|指定)\s*\d{4,6}|^\s*\d{4,6}[A-Z]?\s",text):
        issues.append("文字可能指定股票代號；請改用指定股票範圍，輸入框只保留技術條件。")
    return issues


def rule_issues(raw):
    problems=[]
    def visit(node):
        if "logic" not in node:
            refs={node["field"]}
            if isinstance(node["value"],dict): refs.add(node["value"]["field"])
            if refs & UNITS_UNCONFIRMED:
                problems.append("成交量絕對值／OBV 的資料單位尚未在此介面確認，請先改用成交量倍數條件。")
            return
        for child in node["rules"]: visit(child)
        if node["logic"]!="AND": return
        leaves=[]
        def flatten(n):
            if "logic" not in n: leaves.append(n)
            elif n["logic"]=="AND":
                for c in n["rules"]: flatten(c)
        flatten(node)
        bounds={}
        for leaf in leaves:
            f,op,v=leaf["field"],leaf["operator"],leaf["value"]
            if op in {"cross_above","cross_below"} or isinstance(v,dict): continue
            b=bounds.setdefault(f,dict(low=float("-inf"),high=float("inf"),lo=False,hi=False,eq=[],neq=[]))
            if op=="==": b["eq"].append(v)
            elif op=="!=": b["neq"].append(v)
            if type(v) not in (int,float) and not isinstance(v,list): continue
            if op in (">",">=","==","between"):
                low=v[0] if isinstance(v,list) else v
                if low>b["low"]: b.update(low=low,lo=op==">")
                elif low==b["low"]: b["lo"]|=op==">"
            if op in ("<","<=","==","between"):
                high=v[1] if isinstance(v,list) else v
                if high<b["high"]: b.update(high=high,hi=op=="<")
                elif high==b["high"]: b["hi"]|=op=="<"
        for f,b in bounds.items():
            conflict=(b["low"]>b["high"] or b["low"]==b["high"] and (b["lo"] or b["hi"]) or
                any(v!=b["eq"][0] for v in b["eq"]) or any(v in b["neq"] for v in b["eq"]) or
                b["low"]==b["high"] and b["low"] in b["neq"])
            if conflict: problems.append(f"同一組全部符合條件中的 {f} 門檻互相矛盾；請修改，不會自動刪除條件。")
    visit(raw)
    return list(dict.fromkeys(problems))
