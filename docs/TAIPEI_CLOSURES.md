# 臺北市停班停課名單：下載與自行新增

此功能先建立獨立、可維護的**天然災害停班停課**紀錄，不修改 MySQL、群益連線、回補進度或正式交易日曆。不包含一般國定假日或疫情停課，停班停課也不能直接推論台股休市。

官方來源：[臺北市歷次天然災害停止上班上課訊息](https://data.taipei/dataset/detail?id=83b013c2-35f3-4470-98d7-03dd68a372cb)，提供機關為臺北市政府人事處。該資料集按年更新，下載日不等於已完整收錄至該日。2026-10-24 查詢時，原檔更新時間為 2026-07-16，最後一筆紀錄為 2026-07-12；2026-10-23、10-24 另外依市府新聞稿人工查證補登。

## 檔案用途

- `data/taipei_closures_official.json`：完整官方下載快照與原文、下載時間、來源及 SHA-256。重新下載只更新這個官方快照。
- `config/taipei_closures_manual.json`：**你可以自行新增、修正或停用的紀錄**。下載不改寫此檔。
- `data/taipei_closures.json`：官方與手動資料合併、依查詢範圍篩選後的結果。
- [TAIPEI_CLOSURES_DATES.md](TAIPEI_CLOSURES_DATES.md)：可直接閱讀的日期表；請勿在此新增，因為重新整理會重建此表。

## 下載／整理最近十年

在 `capital_api` 目錄執行，不需啟動 API 或登入：

```powershell
.\.venv\Scripts\python.exe -B -X utf8 scripts\manage_taipei_closures.py refresh
```

預設以臺北當天往前十年，起迄皆包含。以本次為例，範圍為 **2016-10-24～2026-10-24**，不是只取 2017～2026 年。指定基準日可加 `--as-of 2026-10-24`，閏日向前推遇到非閏年時使用 2/28。

官方欄位變動、空回應、非法日期、下載失敗、筆數或最後日期倒退，均中止更新並保留既有官方快照。公告文字無法分類時保留原文，列為「待人工確認」，不列入全市停班停課。正常上班上課與「未達標準」不列入停班停課日期。

## 用指令自行新增日期

全市全天停班停課（以下使用**已查證存在的日期**示範；相同日期／範圍／時段會更新原項目，不會重複新增）：

```powershell
.\.venv\Scripts\python.exe -B -X utf8 scripts\manage_taipei_closures.py add 2026-10-23 --reason "薔琵颱風" --source-url "https://www.gov.taipei/News_Content.aspx?n=F0DDAF49B89E9413&s=111366D9B3B4EC9D&sms=72544237BBE4C5F6" --description "市府宣布全市全天停班停課"
```

局部地區／指定學校，務必明確標示，不能當成全市：

```powershell
.\.venv\Scripts\python.exe -B -X utf8 scripts\manage_taipei_closures.py add 2025-10-23 --reason "1020豪雨" --scope specific_schools --description "臺北市士林區陽明山國民小學停止上班、停止上課" --source-url "https://data.taipei/dataset/detail?id=83b013c2-35f3-4470-98d7-03dd68a372cb"
```

只有停課而照常上班，加 `--classes-only`。部分時段用 `--period morning`、`afternoon`、`evening` 或 `partial`，並以 `--description` 寫出開始時間。局部地區使用 `--scope partial_area`，指定學校使用 `specific_schools`，全市使用 `citywide`。目前不接受把尚未發生的未來預告寫成歷史紀錄。

指令只維護本地人工紀錄，**不會代你查證提供的理由／網址，也不會視為交易所休市公告**。未填來源網址的人工紀錄可以保存，但請盡量補上官方依據。

## 直接編輯手動新增檔

也可以在 `config/taipei_closures_manual.json` 的 `records` 陣列新增物件。欄位範例（JSON 不支援註解）：

```json
{
  "date": "2026-10-23",
  "reason": "薔琵颱風",
  "scope": "citywide",
  "period": "full_day",
  "work_suspended": true,
  "classes_suspended": true,
  "description": "人工查證的公告摘要",
  "source_url": "https://www.gov.taipei/News_Content.aspx?n=F0DDAF49B89E9413&s=111366D9B3B4EC9D&sms=72544237BBE4C5F6",
  "enabled": true
}
```

同一 `date + scope + period` 只能有一筆人工紀錄。人工紀錄優先於相同鍵的官方紀錄，但保留官方原始快照；不同範圍或時段不會互相覆蓋。`enabled: false` 可以抑制**同一鍵**的官方或人工紀錄，其他鍵不受影響。若要更正範圍／時段，先以停用紀錄抑制舊鍵，再新增正確鍵。若只是要取消人工新增而且官方沒有同鍵，直接移除此人工項目即可。

手動編輯後，離線重建結果（不重新下載）：

```powershell
.\.venv\Scripts\python.exe -B -X utf8 scripts\manage_taipei_closures.py build
```

查看合併結果、不改任何檔案：

```powershell
.\.venv\Scripts\python.exe -B -X utf8 scripts\manage_taipei_closures.py list
```

若 JSON 格式、欄位或日期有誤，會明確報錯，不會默默忽略人工紀錄。修改人工檔後須執行 `build`，否則合併結果檔仍是上次版本。重新下載也會同時重建。

## 分類限制

「全市全天」清單只納入明確全市、全天、同時停班與停課的公告；保留週末，因為此清單不是交易日曆。其餘全市部分時段、指定學校、局部地區都另外列示。原文與範圍必須一併看，尤其 2025-10-23 是指定學校，不能與 2026-10-23 的全市公告混淆。

本階段不自動排程更新；需要時手動執行 `refresh`。未接入現有回補程式，完整性驗證仍依先前設定延後。

## 本次確認（2026-10-24）

成功下載官方原檔 126 筆，保留原文與 SHA-256；合併已查證的 10/23、10/24 官方公告補登後，本次十年範圍取得 24 個有停班／停課的日期，其中 14 日全市全天、10 日局部或部分時段，範圍內無未分類紀錄。此為目前來源可取得的紀錄，不保證年更資料缺口期間沒有其他公告。

新增功能的 22 項離線測試及整個專案 713 項離線回歸測試均通過。驗證包含民國日期／閏日、起迄邊界、只停課、局部地區、部分時段、2025 與 2026 同月同日不混淆、手動覆蓋／停用／保留來源、重新整理不改寫手動檔、空資料及下載失敗保留舊檔。測試封鎖真實 MySQL、COM、DLL，沒有操作正式回補。
