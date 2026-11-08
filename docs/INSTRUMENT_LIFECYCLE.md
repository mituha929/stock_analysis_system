# 商品生命週期與缺口分類

## 儲存與來源

沿用現有 MySQL，沒有新增 SQLite 或另一個正式資料庫。

- 股票／ETF／存託憑證沿用 `stock_master`；興櫃沿用官方掛牌日期快照。
- 可轉／交換債新增同庫的 `instrument_lifecycle` 表：`listing_date`、`maturity_date`、`termination_date`、`trading_status`、來源與原始內容。不是把原本未定義格式的 `instruments.expiry_date` 當成到期證據。
- 發行資料：[TPEx bond_ISSBD5_data](https://www.tpex.org.tw/openapi/v1/bond_ISSBD5_data)。使用 `ListingDate`，不是發行日、轉換起日或賣回權日期。
- 下櫃日期：[TPEx 最近下櫃轉交換公司債](https://www.tpex.org.tw/zh-tw/bond/issue/cbond/delisted.html)。此表沒有具體終止原因，不能假稱每筆都是提前贖回或全數轉換。

官方資料每天每程序最多取得一次；請求有逾時與大小上限，且在 SQL 交易外執行。單一來源格式變動時，停止該來源更新、記錄警告，既有日期與終止證據保留。現在的下櫃表不是所有歷史下櫃債券的完整名冊；查不到不能當成仍在交易或已終止。

## 自動抓取規則

1. 掛牌日包含在內，只有掛牌前不查、不列缺口、不建立 `0/5`。週／月 K 的請求從實際掛牌日開始，但保留掛牌所在週／月的 K 線標記。
2. 到期日當日不單靠到期日排除；台北日期晚於到期日，停止該商品所有新自動行情請求，即使保存的計畫截止日較早。人工讀取已儲存歷史資料不受影響。
3. 官方下櫃日視為第一個不交易日；有明確下櫃日，從當日起停止自動查詢。即使原始到期日還沒到，也會停止。
4. 發行資料的餘額必須為零，並同時有已生效變動日與明確轉換／贖回原因，才使用 `fully_converted`／`redeemed` 停止排程。部分轉換、部分買回、停止轉換或賣回權條款都不算已終止。沒有精確終止日，不自行推測應刪除哪些歷史日期。
5. 新規劃、日常回抓、舊斷點、休市日日 K 驗收及來源日 K 彙整共用檢查。已到期／終止的舊工作保留為 `superseded` 並附生命週期原因，**不是 `completed`**。
6. 舊空回應紀錄只有整段落在掛牌前、到期後、下櫃日起，才排除為 `confirmed_no_bar` 並保留原因。仍有合法待查日期的工作不會被整筆解除等待。
7. 不刪除、清零或重寫既有掛牌前／到期後行情。轉上市股票的原上櫃歷史與既有保護規則仍保留；資料不足仍標示不足，不宣稱補滿目標。

上述規則針對自動回補／排程。原有明確指定日期的單檔直接歷史查詢 API 是人工診斷入口，不因本次修改被禁止，亦不會自動加入缺口觀察。

## 有成交但缺 K，與尚無成交證據

- `api_missing_confirmed`：同一商品、同一市場型態、該日期已有 `capital_history` 來源且成交量大於零的日／分 K，但該次請求仍缺整日或整個週／月 K，列 API 缺漏調查。
- `trade_activity_unconfirmed`：沒有上述正面成交證據，保留交易活動待查；不認定為 API 漏資料，也不認定無交易。
- 整張與零股分開。週／月成交量不能證明其中某一天有交易，某天有成交也不代表每一分鐘都應有 K。
- 這裡的正面證據是系統已取得的實際正成交量行情，不是額外完整爬取官方逐日成交表。沒有其他週期資料的商品會保守留在待查，不能宣稱已核實是否成交。
- 空回應觀察達 3～5 個不同交易日後仍是待查，不自動成為無交易證據。

## 使用與驗證

在 `capital_api` 專案目錄先唯讀預覽（不讀 `.env`、不寫 DB、不登入群益）：

```powershell
.\.venv\Scripts\python.exe -B -X utf8 scripts\sync_bond_lifecycle.py --symbol 17272 --symbol 23161
```

新程式啟動回補時，自動建立同庫的附加資料表並同步日期，不需要重新抓取既有行情。也可在沒有回補持有工作鎖時，只同步生命週期中繼資料：

```powershell
.\.venv\Scripts\python.exe -B -X utf8 scripts\sync_bond_lifecycle.py --apply
```

`--apply` 不登入、不重啟、不動行情與斷點；排除舊等待由之後正常回補持有工作鎖時處理。不要以 `--force` 或全市場重抓來套用。

離線測試：

```powershell
.\.venv\Scripts\python.exe -B -X utf8 scripts\run_worker_tests_offline.py
```

隔離 MySQL 測試（會新建 UUID 測試庫並在結束後刪除，只使用假群益行情）：

```powershell
.\.venv\Scripts\python.exe -B -X utf8 scripts\test_lifecycle_mysql_isolated.py
```
