# 台股休市日期（上市／上櫃／興櫃共用）

範圍：2016-10-26～2026-10-26（含起迄日）；整理時間：2026-10-26T00:49:02.595505+08:00。

已收錄休市 1217 日：平日 178 日、週末 1039 日（同日不重複計算）。

**完整性限制：** 年度表不保證收錄臨時休市；臨時事件僅依另附交易所公告補登。缺年度的未列日期不代表有交易；2019 年前週六保留補行交易例外。
此操作不修改 .env、MySQL 或回補工作；執行期交易日判斷會讀取此合併快照。人工維護見 [操作說明](STOCK_MARKET_HOLIDAYS.md)。

## 年度來源與缺漏

| 年度 | 狀態 | 平日休市 | 週末休市 | 來源 |
| --- | --- | ---: | ---: | --- |
| 2016 | 官方舊年表已人工核對 | 0 | 19 | [官方來源](https://www.tpex.org.tw/storage/en-us/web/bulletin/trading_date/trading_date_105.htm) |
| 2017 | 官方舊年表已人工核對 | 17 | 102 | [官方來源](https://www.tpex.org.tw/storage/en-us/web/bulletin/trading_date/trading_date_106.htm) |
| 2018 | 官方舊年表已人工核對 | 16 | 102 | [官方來源](https://www.tpex.org.tw/storage/en-us/web/bulletin/trading_date/trading_date_107.htm) |
| 2019 | 官方舊年表已人工核對 | 19 | 104 | [官方來源](https://www.tpex.org.tw/storage/en-us/web/bulletin/trading_date/trading_date_108.htm) |
| 2020 | 官方舊年表已人工核對 | 17 | 104 | [官方來源](https://www.tpex.org.tw/storage/en-us/web/bulletin/trading_date/trading_date_109.htm) |
| 2021 | 官方 API 已取得 | 17 | 104 | [官方來源](https://wwwc.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule?date=20210101&response=json) |
| 2022 | 官方 API 已取得 | 14 | 105 | [官方來源](https://wwwc.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule?date=20220101&response=json) |
| 2023 | 官方 API 已取得 | 21 | 105 | [官方來源](https://wwwc.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule?date=20230101&response=json) |
| 2024 | 官方 API 已取得 | 20 | 104 | [官方來源](https://wwwc.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule?date=20240101&response=json) |
| 2025 | 官方 API 已取得 | 18 | 104 | [官方來源](https://wwwc.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule?date=20250101&response=json) |
| 2026 | 官方 API 已取得 | 19 | 86 | [官方來源](https://wwwc.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule?date=20260101&response=json) |

## 週六補行交易例外（不列入休市）

2017-02-18、2017-06-03、2017-09-30、2018-03-31、2018-12-22

## 臨時休市來源層級

年度表之外的休市逐筆附來源，不直接套用臺北市停班停課清單。來源為中央社轉述或公會公告時，保留該層級，不冒稱交易所原始公告。
臨時休市公告的歷史完整性未宣稱已全部驗證，原始資料保留 emergency_coverage_complete=false。

## 公告休市日期（含與週末重疊的公告，非全部週末）

| 日期 | 類型 | 名稱 | 說明 | 來源 |
| --- | --- | --- | --- | --- |
| 2017-01-01 | annual_holiday | 中華民國開國紀念日／補假 | Republic of China Memorial Day | [來源](https://www.tpex.org.tw/storage/en-us/web/bulletin/trading_date/trading_date_106.htm) |
| 2017-01-02 | annual_holiday | 中華民國開國紀念日／補假 | Republic of China Memorial Day | [來源](https://www.tpex.org.tw/storage/en-us/web/bulletin/trading_date/trading_date_106.htm) |
| 2017-01-25 | annual_holiday | 市場無交易，僅辦理結算交割 | Last Clearing & Settlement Days before Lunar New Year Holiday | [來源](https://www.tpex.org.tw/storage/en-us/web/bulletin/trading_date/trading_date_106.htm) |
| 2017-01-26 | annual_holiday | 市場無交易，僅辦理結算交割 | Last Clearing & Settlement Days before Lunar New Year Holiday | [來源](https://www.tpex.org.tw/storage/en-us/web/bulletin/trading_date/trading_date_106.htm) |
| 2017-01-27 | annual_holiday | 農曆除夕 | Lunar New Year's Eve | [來源](https://www.tpex.org.tw/storage/en-us/web/bulletin/trading_date/trading_date_106.htm) |
| 2017-01-28 | annual_holiday | 農曆春節／補假 | Lunar New Year Holiday (Market reopens on February 2) | [來源](https://www.tpex.org.tw/storage/en-us/web/bulletin/trading_date/trading_date_106.htm) |
| 2017-01-29 | annual_holiday | 農曆春節／補假 | Lunar New Year Holiday (Market reopens on February 2) | [來源](https://www.tpex.org.tw/storage/en-us/web/bulletin/trading_date/trading_date_106.htm) |
| 2017-01-30 | annual_holiday | 農曆春節／補假 | Lunar New Year Holiday (Market reopens on February 2) | [來源](https://www.tpex.org.tw/storage/en-us/web/bulletin/trading_date/trading_date_106.htm) |
| 2017-01-31 | annual_holiday | 農曆春節／補假 | Lunar New Year Holiday (Market reopens on February 2) | [來源](https://www.tpex.org.tw/storage/en-us/web/bulletin/trading_date/trading_date_106.htm) |
| 2017-02-01 | annual_holiday | 農曆春節／補假 | Lunar New Year Holiday (Market reopens on February 2) | [來源](https://www.tpex.org.tw/storage/en-us/web/bulletin/trading_date/trading_date_106.htm) |
| 2017-02-27 | annual_holiday | 和平紀念日／調整放假 | Adjusted Holiday; Peace Memorial Day | [來源](https://www.tpex.org.tw/storage/en-us/web/bulletin/trading_date/trading_date_106.htm) |
| 2017-02-28 | annual_holiday | 和平紀念日／調整放假 | Adjusted Holiday; Peace Memorial Day | [來源](https://www.tpex.org.tw/storage/en-us/web/bulletin/trading_date/trading_date_106.htm) |
| 2017-04-03 | annual_holiday | 兒童節、民族掃墓節／調整放假 | Adjusted Holiday; Children's Day; Tomb-Sweeping Day | [來源](https://www.tpex.org.tw/storage/en-us/web/bulletin/trading_date/trading_date_106.htm) |
| 2017-04-04 | annual_holiday | 兒童節、民族掃墓節／調整放假 | Adjusted Holiday; Children's Day; Tomb-Sweeping Day | [來源](https://www.tpex.org.tw/storage/en-us/web/bulletin/trading_date/trading_date_106.htm) |
| 2017-05-01 | annual_holiday | 勞動節 | Labor Day | [來源](https://www.tpex.org.tw/storage/en-us/web/bulletin/trading_date/trading_date_106.htm) |
| 2017-05-29 | annual_holiday | 端午節／調整放假 | Adjusted Holiday; Dragon Boat Festival | [來源](https://www.tpex.org.tw/storage/en-us/web/bulletin/trading_date/trading_date_106.htm) |
| 2017-05-30 | annual_holiday | 端午節／調整放假 | Adjusted Holiday; Dragon Boat Festival | [來源](https://www.tpex.org.tw/storage/en-us/web/bulletin/trading_date/trading_date_106.htm) |
| 2017-10-04 | annual_holiday | 中秋節 | Moon Festival | [來源](https://www.tpex.org.tw/storage/en-us/web/bulletin/trading_date/trading_date_106.htm) |
| 2017-10-09 | annual_holiday | 國慶日／調整放假 | Adjusted Holiday; National Day | [來源](https://www.tpex.org.tw/storage/en-us/web/bulletin/trading_date/trading_date_106.htm) |
| 2017-10-10 | annual_holiday | 國慶日／調整放假 | Adjusted Holiday; National Day | [來源](https://www.tpex.org.tw/storage/en-us/web/bulletin/trading_date/trading_date_106.htm) |
| 2018-01-01 | annual_holiday | 中華民國開國紀念日 | Republic of China Memorial Day | [來源](https://www.tpex.org.tw/storage/en-us/web/bulletin/trading_date/trading_date_107.htm) |
| 2018-02-13 | annual_holiday | 市場無交易，僅辦理結算交割 | Last Clearing & Settlement Days before Lunar New Year Holiday；原英文表 2/13 星期文字誤植，採日期並與 TWSE 官方 2018 年表交叉核對 | [來源](https://www.tpex.org.tw/storage/en-us/web/bulletin/trading_date/trading_date_107.htm) |
| 2018-02-14 | annual_holiday | 市場無交易，僅辦理結算交割 | Last Clearing & Settlement Days before Lunar New Year Holiday；原英文表 2/13 星期文字誤植，採日期並與 TWSE 官方 2018 年表交叉核對 | [來源](https://www.tpex.org.tw/storage/en-us/web/bulletin/trading_date/trading_date_107.htm) |
| 2018-02-15 | annual_holiday | 農曆除夕 | Lunar New Year's Eve | [來源](https://www.tpex.org.tw/storage/en-us/web/bulletin/trading_date/trading_date_107.htm) |
| 2018-02-16 | annual_holiday | 農曆春節／補假 | Lunar New Year Holiday (Market reopens on February 21) | [來源](https://www.tpex.org.tw/storage/en-us/web/bulletin/trading_date/trading_date_107.htm) |
| 2018-02-17 | annual_holiday | 農曆春節／補假 | Lunar New Year Holiday (Market reopens on February 21) | [來源](https://www.tpex.org.tw/storage/en-us/web/bulletin/trading_date/trading_date_107.htm) |
| 2018-02-18 | annual_holiday | 農曆春節／補假 | Lunar New Year Holiday (Market reopens on February 21) | [來源](https://www.tpex.org.tw/storage/en-us/web/bulletin/trading_date/trading_date_107.htm) |
| 2018-02-19 | annual_holiday | 農曆春節／補假 | Lunar New Year Holiday (Market reopens on February 21) | [來源](https://www.tpex.org.tw/storage/en-us/web/bulletin/trading_date/trading_date_107.htm) |
| 2018-02-20 | annual_holiday | 農曆春節／補假 | Lunar New Year Holiday (Market reopens on February 21) | [來源](https://www.tpex.org.tw/storage/en-us/web/bulletin/trading_date/trading_date_107.htm) |
| 2018-02-28 | annual_holiday | 和平紀念日 | Peace Memorial Day | [來源](https://www.tpex.org.tw/storage/en-us/web/bulletin/trading_date/trading_date_107.htm) |
| 2018-04-04 | annual_holiday | 兒童節 | Children's Day | [來源](https://www.tpex.org.tw/storage/en-us/web/bulletin/trading_date/trading_date_107.htm) |
| 2018-04-05 | annual_holiday | 民族掃墓節／調整放假 | Tomb-Sweeping Day; Adjusted Holiday | [來源](https://www.tpex.org.tw/storage/en-us/web/bulletin/trading_date/trading_date_107.htm) |
| 2018-04-06 | annual_holiday | 民族掃墓節／調整放假 | Tomb-Sweeping Day; Adjusted Holiday | [來源](https://www.tpex.org.tw/storage/en-us/web/bulletin/trading_date/trading_date_107.htm) |
| 2018-05-01 | annual_holiday | 勞動節 | Labor Day | [來源](https://www.tpex.org.tw/storage/en-us/web/bulletin/trading_date/trading_date_107.htm) |
| 2018-06-18 | annual_holiday | 端午節 | Dragon Boat Festival | [來源](https://www.tpex.org.tw/storage/en-us/web/bulletin/trading_date/trading_date_107.htm) |
| 2018-09-24 | annual_holiday | 中秋節 | Moon Festival | [來源](https://www.tpex.org.tw/storage/en-us/web/bulletin/trading_date/trading_date_107.htm) |
| 2018-10-10 | annual_holiday | 國慶日 | National Day | [來源](https://www.tpex.org.tw/storage/en-us/web/bulletin/trading_date/trading_date_107.htm) |
| 2018-12-31 | annual_holiday | 元旦前調整放假 | Adjusted Holiday before Republic of China Memorial Day 2019 | [來源](https://www.tpex.org.tw/storage/en-us/web/bulletin/trading_date/trading_date_107.htm) |
| 2019-01-01 | annual_holiday | 中華民國開國紀念日 | Republic of China Memorial Day | [來源](https://www.tpex.org.tw/storage/en-us/web/bulletin/trading_date/trading_date_108.htm) |
| 2019-01-31 | annual_holiday | 市場無交易，僅辦理結算交割 | Last Clearing & Settlement Days before Lunar New Year Holiday | [來源](https://www.tpex.org.tw/storage/en-us/web/bulletin/trading_date/trading_date_108.htm) |
| 2019-02-01 | annual_holiday | 市場無交易，僅辦理結算交割 | Last Clearing & Settlement Days before Lunar New Year Holiday | [來源](https://www.tpex.org.tw/storage/en-us/web/bulletin/trading_date/trading_date_108.htm) |
| 2019-02-04 | annual_holiday | 農曆除夕 | Lunar New Year's Eve | [來源](https://www.tpex.org.tw/storage/en-us/web/bulletin/trading_date/trading_date_108.htm) |
| 2019-02-05 | annual_holiday | 農曆春節／調整放假 | Lunar New Year Holiday (Market reopens on February 11) | [來源](https://www.tpex.org.tw/storage/en-us/web/bulletin/trading_date/trading_date_108.htm) |
| 2019-02-06 | annual_holiday | 農曆春節／調整放假 | Lunar New Year Holiday (Market reopens on February 11) | [來源](https://www.tpex.org.tw/storage/en-us/web/bulletin/trading_date/trading_date_108.htm) |
| 2019-02-07 | annual_holiday | 農曆春節／調整放假 | Lunar New Year Holiday (Market reopens on February 11) | [來源](https://www.tpex.org.tw/storage/en-us/web/bulletin/trading_date/trading_date_108.htm) |
| 2019-02-08 | annual_holiday | 農曆春節／調整放假 | Lunar New Year Holiday (Market reopens on February 11) | [來源](https://www.tpex.org.tw/storage/en-us/web/bulletin/trading_date/trading_date_108.htm) |
| 2019-02-28 | annual_holiday | 和平紀念日／調整放假 | Peace Memorial Day; Adjusted Holiday | [來源](https://www.tpex.org.tw/storage/en-us/web/bulletin/trading_date/trading_date_108.htm) |
| 2019-03-01 | annual_holiday | 和平紀念日／調整放假 | Peace Memorial Day; Adjusted Holiday | [來源](https://www.tpex.org.tw/storage/en-us/web/bulletin/trading_date/trading_date_108.htm) |
| 2019-04-04 | annual_holiday | 兒童節 | Children's Day | [來源](https://www.tpex.org.tw/storage/en-us/web/bulletin/trading_date/trading_date_108.htm) |
| 2019-04-05 | annual_holiday | 民族掃墓節 | Tomb-Sweeping Day | [來源](https://www.tpex.org.tw/storage/en-us/web/bulletin/trading_date/trading_date_108.htm) |
| 2019-05-01 | annual_holiday | 勞動節 | Labor Day | [來源](https://www.tpex.org.tw/storage/en-us/web/bulletin/trading_date/trading_date_108.htm) |
| 2019-06-07 | annual_holiday | 端午節 | Dragon Boat Festival | [來源](https://www.tpex.org.tw/storage/en-us/web/bulletin/trading_date/trading_date_108.htm) |
| 2019-08-09 | emergency_closure | 利奇馬颱風臨時休市 | 中央社引述證交所、櫃買中心公告全日休市，並明列含興櫃股票；屬交易所公告的媒體轉述，非交易所原始頁，來源層級另標示。 | [來源](https://www.cna.com.tw/news/afe/201908080362.aspx) |
| 2019-09-13 | annual_holiday | 中秋節 | Moon Festival | [來源](https://www.tpex.org.tw/storage/en-us/web/bulletin/trading_date/trading_date_108.htm) |
| 2019-09-30 | emergency_closure | 米塔颱風臨時休市 | 中央社引述證交所、櫃買中心公告全日休市，並明列含興櫃股票；屬交易所公告的媒體轉述，非交易所原始頁，來源層級另標示。 | [來源](https://www.cna.com.tw/news/afe/201909290209.aspx) |
| 2019-10-10 | annual_holiday | 國慶日／調整放假 | National Day; Adjusted Holiday | [來源](https://www.tpex.org.tw/storage/en-us/web/bulletin/trading_date/trading_date_108.htm) |
| 2019-10-11 | annual_holiday | 國慶日／調整放假 | National Day; Adjusted Holiday | [來源](https://www.tpex.org.tw/storage/en-us/web/bulletin/trading_date/trading_date_108.htm) |
| 2020-01-01 | annual_holiday | 中華民國開國紀念日 | Republic of China Memorial Day | [來源](https://www.tpex.org.tw/storage/en-us/web/bulletin/trading_date/trading_date_109.htm) |
| 2020-01-21 | annual_holiday | 市場無交易，僅辦理結算交割 | Last Clearing & Settlement Days before Lunar New Year Holiday | [來源](https://www.tpex.org.tw/storage/en-us/web/bulletin/trading_date/trading_date_109.htm) |
| 2020-01-22 | annual_holiday | 市場無交易，僅辦理結算交割 | Last Clearing & Settlement Days before Lunar New Year Holiday | [來源](https://www.tpex.org.tw/storage/en-us/web/bulletin/trading_date/trading_date_109.htm) |
| 2020-01-23 | annual_holiday | 春節前調整放假 | Adjusted Holiday | [來源](https://www.tpex.org.tw/storage/en-us/web/bulletin/trading_date/trading_date_109.htm) |
| 2020-01-24 | annual_holiday | 農曆除夕 | Lunar New Year's Eve | [來源](https://www.tpex.org.tw/storage/en-us/web/bulletin/trading_date/trading_date_109.htm) |
| 2020-01-25 | annual_holiday | 農曆春節／補假 | Lunar New Year Holiday (Market reopens on January 30) | [來源](https://www.tpex.org.tw/storage/en-us/web/bulletin/trading_date/trading_date_109.htm) |
| 2020-01-26 | annual_holiday | 農曆春節／補假 | Lunar New Year Holiday (Market reopens on January 30) | [來源](https://www.tpex.org.tw/storage/en-us/web/bulletin/trading_date/trading_date_109.htm) |
| 2020-01-27 | annual_holiday | 農曆春節／補假 | Lunar New Year Holiday (Market reopens on January 30) | [來源](https://www.tpex.org.tw/storage/en-us/web/bulletin/trading_date/trading_date_109.htm) |
| 2020-01-28 | annual_holiday | 農曆春節／補假 | Lunar New Year Holiday (Market reopens on January 30) | [來源](https://www.tpex.org.tw/storage/en-us/web/bulletin/trading_date/trading_date_109.htm) |
| 2020-01-29 | annual_holiday | 農曆春節／補假 | Lunar New Year Holiday (Market reopens on January 30) | [來源](https://www.tpex.org.tw/storage/en-us/web/bulletin/trading_date/trading_date_109.htm) |
| 2020-02-28 | annual_holiday | 和平紀念日 | Peace Memorial Day | [來源](https://www.tpex.org.tw/storage/en-us/web/bulletin/trading_date/trading_date_109.htm) |
| 2020-04-02 | annual_holiday | 兒童節、民族掃墓節／補假 | Adjusted Holiday; Children's Day; Tomb-Sweeping Day | [來源](https://www.tpex.org.tw/storage/en-us/web/bulletin/trading_date/trading_date_109.htm) |
| 2020-04-03 | annual_holiday | 兒童節、民族掃墓節／補假 | Adjusted Holiday; Children's Day; Tomb-Sweeping Day | [來源](https://www.tpex.org.tw/storage/en-us/web/bulletin/trading_date/trading_date_109.htm) |
| 2020-04-04 | annual_holiday | 兒童節、民族掃墓節／補假 | Adjusted Holiday; Children's Day; Tomb-Sweeping Day | [來源](https://www.tpex.org.tw/storage/en-us/web/bulletin/trading_date/trading_date_109.htm) |
| 2020-05-01 | annual_holiday | 勞動節 | Labor Day | [來源](https://www.tpex.org.tw/storage/en-us/web/bulletin/trading_date/trading_date_109.htm) |
| 2020-06-25 | annual_holiday | 端午節／調整放假 | Dragon Boat Festival; Adjusted Holiday | [來源](https://www.tpex.org.tw/storage/en-us/web/bulletin/trading_date/trading_date_109.htm) |
| 2020-06-26 | annual_holiday | 端午節／調整放假 | Dragon Boat Festival; Adjusted Holiday | [來源](https://www.tpex.org.tw/storage/en-us/web/bulletin/trading_date/trading_date_109.htm) |
| 2020-10-01 | annual_holiday | 中秋節／調整放假 | Moon Festival; Adjusted Holiday | [來源](https://www.tpex.org.tw/storage/en-us/web/bulletin/trading_date/trading_date_109.htm) |
| 2020-10-02 | annual_holiday | 中秋節／調整放假 | Moon Festival; Adjusted Holiday | [來源](https://www.tpex.org.tw/storage/en-us/web/bulletin/trading_date/trading_date_109.htm) |
| 2020-10-09 | annual_holiday | 國慶日／補假 | Adjusted Holiday; National Day | [來源](https://www.tpex.org.tw/storage/en-us/web/bulletin/trading_date/trading_date_109.htm) |
| 2020-10-10 | annual_holiday | 國慶日／補假 | Adjusted Holiday; National Day | [來源](https://www.tpex.org.tw/storage/en-us/web/bulletin/trading_date/trading_date_109.htm) |
| 2021-01-01 | annual_holiday | 中華民國開國紀念日 | 依規定放假1日。 | [來源](https://wwwc.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule?date=20210101&response=json) |
| 2021-02-08 | annual_holiday | 農曆春節前最後交易日 | 2月8日市場無交易，僅辦理結算交割作業。 | [來源](https://wwwc.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule?date=20210101&response=json) |
| 2021-02-09 | annual_holiday | 農曆春節前最後交易日 | 2月9日市場無交易，僅辦理結算交割作業。 | [來源](https://wwwc.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule?date=20210101&response=json) |
| 2021-02-10 | annual_holiday | 農曆除夕前一日 | 2月10日（星期三）調整放假，於2月20日（星期六）補行上班，但不交易亦不交割。 | [來源](https://wwwc.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule?date=20210101&response=json) |
| 2021-02-11 | annual_holiday | 農曆除夕 | 依規定放假1日。 | [來源](https://wwwc.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule?date=20210101&response=json) |
| 2021-02-12 | annual_holiday | 農曆春節 | 依規定放假1日。 | [來源](https://wwwc.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule?date=20210101&response=json) |
| 2021-02-13 | annual_holiday | 農曆春節 | 依規定放假1日。 | [來源](https://wwwc.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule?date=20210101&response=json) |
| 2021-02-14 | annual_holiday | 農曆春節 | 依規定放假1日。 | [來源](https://wwwc.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule?date=20210101&response=json) |
| 2021-02-15 | annual_holiday | 農曆春節 | 2月13日及2月14日適逢星期六及日，2月15日（星期一）及2月16日（星期二）補假1日。 | [來源](https://wwwc.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule?date=20210101&response=json) |
| 2021-02-16 | annual_holiday | 農曆春節 | 2月13日及2月14日適逢星期六及日，2月15日（星期一）及2月16日（星期二）補假1日。 | [來源](https://wwwc.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule?date=20210101&response=json) |
| 2021-02-28 | annual_holiday | 和平紀念日 | 依規定放假1日。 | [來源](https://wwwc.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule?date=20210101&response=json) |
| 2021-03-01 | annual_holiday | 和平紀念日 | 2月28日適逢星期日，3月1日（星期一）補假1日。 | [來源](https://wwwc.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule?date=20210101&response=json) |
| 2021-04-02 | annual_holiday | 兒童節 民族掃墓節 | 因兒童節與民族掃墓節為同一日(4月4日)且適逢星期日，於前一日4月2日（星期五）放假1日，且4月5日（星期一）補假1日。 | [來源](https://wwwc.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule?date=20210101&response=json) |
| 2021-04-03 | annual_holiday | 兒童節 民族掃墓節 | 依規定放假1日。 | [來源](https://wwwc.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule?date=20210101&response=json) |
| 2021-04-04 | annual_holiday | 兒童節 民族掃墓節 | 兒童節 民族掃墓節依規定放假1日。 | [來源](https://wwwc.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule?date=20210101&response=json) |
| 2021-04-05 | annual_holiday | 兒童節 民族掃墓節 | 因兒童節與民族掃墓節為同一日(4月4日)且適逢星期日，於前一日4月2日（星期五）放假1日，且4月5日（星期一）補假1日。 | [來源](https://wwwc.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule?date=20210101&response=json) |
| 2021-04-30 | annual_holiday | 勞動節 | 5月1日適逢星期六，4月30日（星期五）補假1日。 | [來源](https://wwwc.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule?date=20210101&response=json) |
| 2021-05-01 | annual_holiday | 勞動節 | 依規定放假1日。 | [來源](https://wwwc.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule?date=20210101&response=json) |
| 2021-06-14 | annual_holiday | 端午節 | 依規定放假1日。 | [來源](https://wwwc.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule?date=20210101&response=json) |
| 2021-09-20 | annual_holiday | 中秋節 | 9月20日（星期一）調整放假，於9月11日（星期六）補行上班，但不交易亦不交割。 | [來源](https://wwwc.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule?date=20210101&response=json) |
| 2021-09-21 | annual_holiday | 中秋節 | 依規定放假1日。 | [來源](https://wwwc.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule?date=20210101&response=json) |
| 2021-10-10 | annual_holiday | 國慶日 | 依規定放假1日。 | [來源](https://wwwc.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule?date=20210101&response=json) |
| 2021-10-11 | annual_holiday | 國慶日 | 10月10日適逢星期日，10月11日（星期一）補假1日。 | [來源](https://wwwc.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule?date=20210101&response=json) |
| 2021-12-31 | annual_holiday | 中華民國開國紀念日 | 111年1月1日適逢星期六，110年12月31日（星期五）補假1日。 | [來源](https://wwwc.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule?date=20210101&response=json) |
| 2022-01-01 | annual_holiday | 中華民國開國紀念日 | 依規定放假1日。 | [來源](https://wwwc.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule?date=20220101&response=json) |
| 2022-01-27 | annual_holiday | 農曆春節前最後交易日 | 1月27日市場無交易，僅辦理結算交割作業。 | [來源](https://wwwc.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule?date=20220101&response=json) |
| 2022-01-28 | annual_holiday | 農曆春節前最後交易日 | 1月28日市場無交易，僅辦理結算交割作業。 | [來源](https://wwwc.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule?date=20220101&response=json) |
| 2022-01-31 | annual_holiday | 農曆除夕 | 依規定放假1日。 | [來源](https://wwwc.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule?date=20220101&response=json) |
| 2022-02-01 | annual_holiday | 農曆春節 | 依規定放假1日。 | [來源](https://wwwc.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule?date=20220101&response=json) |
| 2022-02-02 | annual_holiday | 農曆春節 | 依規定放假1日。 | [來源](https://wwwc.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule?date=20220101&response=json) |
| 2022-02-03 | annual_holiday | 農曆春節 | 依規定放假1日。 | [來源](https://wwwc.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule?date=20220101&response=json) |
| 2022-02-04 | annual_holiday | 農曆春節 | 2月4日（星期五）調整放假，於1月22日（星期六）補行上班，但不交易亦不交割。 | [來源](https://wwwc.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule?date=20220101&response=json) |
| 2022-02-28 | annual_holiday | 和平紀念日 | 依規定放假1日。 | [來源](https://wwwc.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule?date=20220101&response=json) |
| 2022-04-04 | annual_holiday | 兒童節 | 依規定放假1日。 | [來源](https://wwwc.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule?date=20220101&response=json) |
| 2022-04-05 | annual_holiday | 民族掃墓節 | 依規定放假1日。 | [來源](https://wwwc.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule?date=20220101&response=json) |
| 2022-05-01 | annual_holiday | 勞動節 | 依規定放假1日。 | [來源](https://wwwc.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule?date=20220101&response=json) |
| 2022-05-02 | annual_holiday | 勞動節 | 5月1日適逢星期日，5月2日（星期一）補假1日。 | [來源](https://wwwc.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule?date=20220101&response=json) |
| 2022-06-03 | annual_holiday | 端午節 | 依規定放假1日。 | [來源](https://wwwc.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule?date=20220101&response=json) |
| 2022-09-09 | annual_holiday | 中秋節 | 9月10日適逢星期六，9月9日（星期五）補假1日。 | [來源](https://wwwc.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule?date=20220101&response=json) |
| 2022-09-10 | annual_holiday | 中秋節 | 依規定放假1日。 | [來源](https://wwwc.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule?date=20220101&response=json) |
| 2022-10-10 | annual_holiday | 國慶日 | 依規定放假1日。 | [來源](https://wwwc.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule?date=20220101&response=json) |
| 2023-01-01 | annual_holiday | 中華民國開國紀念日 | 依規定放假1日。 1月1日適逢星期日，於1月2日（星期一）補假1日。 | [來源](https://wwwc.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule?date=20230101&response=json) |
| 2023-01-02 | annual_holiday | 中華民國開國紀念日 | 依規定放假1日。 1月1日適逢星期日，於1月2日（星期一）補假1日。 | [來源](https://wwwc.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule?date=20230101&response=json) |
| 2023-01-18 | annual_holiday | 市場無交易，僅辦理結算交割作業 |  | [來源](https://wwwc.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule?date=20230101&response=json) |
| 2023-01-19 | annual_holiday | 市場無交易，僅辦理結算交割作業 |  | [來源](https://wwwc.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule?date=20230101&response=json) |
| 2023-01-20 | annual_holiday | 農曆除夕前一日 | 1月20日（星期五）調整放假，於1月7日（星期六）補行上班，但不交易亦不交割。 | [來源](https://wwwc.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule?date=20230101&response=json) |
| 2023-01-21 | annual_holiday | 農曆除夕 | 依規定放假1日。 | [來源](https://wwwc.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule?date=20230101&response=json) |
| 2023-01-22 | annual_holiday | 農曆春節 | 依規定於1月22日至1月24日放假3日。  1月21日及1月22日適逢星期六及星期日，於1月25日（星期三）及1月26日（星期四）補假2日。 1月27日（星期五）調整放假，於2月4日（星期六）補行上班，但不交易亦不交割。 | [來源](https://wwwc.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule?date=20230101&response=json) |
| 2023-01-23 | annual_holiday | 農曆春節 | 依規定於1月22日至1月24日放假3日。  1月21日及1月22日適逢星期六及星期日，於1月25日（星期三）及1月26日（星期四）補假2日。 1月27日（星期五）調整放假，於2月4日（星期六）補行上班，但不交易亦不交割。 | [來源](https://wwwc.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule?date=20230101&response=json) |
| 2023-01-24 | annual_holiday | 農曆春節 | 依規定於1月22日至1月24日放假3日。  1月21日及1月22日適逢星期六及星期日，於1月25日（星期三）及1月26日（星期四）補假2日。 1月27日（星期五）調整放假，於2月4日（星期六）補行上班，但不交易亦不交割。 | [來源](https://wwwc.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule?date=20230101&response=json) |
| 2023-01-25 | annual_holiday | 農曆春節 | 1月21日及1月22日適逢星期六及星期日，於1月25日（星期三）及1月26日（星期四）補假2日。 1月27日（星期五）調整放假，於2月4日（星期六）補行上班，但不交易亦不交割。 | [來源](https://wwwc.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule?date=20230101&response=json) |
| 2023-01-26 | annual_holiday | 農曆春節 | 1月21日及1月22日適逢星期六及星期日，於1月25日（星期三）及1月26日（星期四）補假2日。 1月27日（星期五）調整放假，於2月4日（星期六）補行上班，但不交易亦不交割。 | [來源](https://wwwc.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule?date=20230101&response=json) |
| 2023-01-27 | annual_holiday | 農曆春節 | 1月21日及1月22日適逢星期六及星期日，於1月25日（星期三）及1月26日（星期四）補假2日。 1月27日（星期五）調整放假，於2月4日（星期六）補行上班，但不交易亦不交割。 | [來源](https://wwwc.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule?date=20230101&response=json) |
| 2023-02-27 | annual_holiday | 和平紀念日 | 2月27日（星期一）調整放假，於2月18日（星期六）補行上班，但不交易亦不交割。 依規定放假1日。 | [來源](https://wwwc.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule?date=20230101&response=json) |
| 2023-02-28 | annual_holiday | 和平紀念日 | 2月27日（星期一）調整放假，於2月18日（星期六）補行上班，但不交易亦不交割。 依規定放假1日。 | [來源](https://wwwc.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule?date=20230101&response=json) |
| 2023-04-03 | annual_holiday | 兒童節 | 4月3日（星期一）調整放假，於3月25日（星期六）補行上班，但不交易亦不交割。 依規定放假1日。 | [來源](https://wwwc.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule?date=20230101&response=json) |
| 2023-04-04 | annual_holiday | 兒童節 | 4月3日（星期一）調整放假，於3月25日（星期六）補行上班，但不交易亦不交割。 依規定放假1日。 | [來源](https://wwwc.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule?date=20230101&response=json) |
| 2023-04-05 | annual_holiday | 民族掃墓節 | 依規定放假1日。 | [來源](https://wwwc.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule?date=20230101&response=json) |
| 2023-05-01 | annual_holiday | 勞動節 | 依規定放假1日。 | [來源](https://wwwc.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule?date=20230101&response=json) |
| 2023-06-22 | annual_holiday | 端午節 | 依規定放假1日。 6月23日（星期五）調整放假，於6月17日（星期六）補行上班，但不交易亦不交割。 | [來源](https://wwwc.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule?date=20230101&response=json) |
| 2023-06-23 | annual_holiday | 端午節 | 依規定放假1日。 6月23日（星期五）調整放假，於6月17日（星期六）補行上班，但不交易亦不交割。 | [來源](https://wwwc.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule?date=20230101&response=json) |
| 2023-08-03 | emergency_closure | 卡努颱風臨時休市 | 證交所 112 年 8 月 2 日新聞稿明列次日集中市場全日休市。 | [來源](https://investoredu.twse.com.tw/Mobile_pages/..%2FFileSystem%2FFileUpload%2Fbe0109d1-76a0-4412-a00d-58a88c6e1714.pdf) |
| 2023-09-29 | annual_holiday | 中秋節 | 依規定放假1日。 | [來源](https://wwwc.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule?date=20230101&response=json) |
| 2023-10-09 | annual_holiday | 國慶日 | 10月9日（星期一）調整放假，於9月23日（星期六）補行上班，但不交易亦不交割。 依規定放假1日。 | [來源](https://wwwc.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule?date=20230101&response=json) |
| 2023-10-10 | annual_holiday | 國慶日 | 10月9日（星期一）調整放假，於9月23日（星期六）補行上班，但不交易亦不交割。 依規定放假1日。 | [來源](https://wwwc.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule?date=20230101&response=json) |
| 2024-01-01 | annual_holiday | 中華民國開國紀念日 | 依規定放假1日。 | [來源](https://wwwc.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule?date=20240101&response=json) |
| 2024-02-06 | annual_holiday | 市場無交易，僅辦理結算交割作業 |  | [來源](https://wwwc.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule?date=20240101&response=json) |
| 2024-02-07 | annual_holiday | 市場無交易，僅辦理結算交割作業 |  | [來源](https://wwwc.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule?date=20240101&response=json) |
| 2024-02-08 | annual_holiday | 農曆除夕前一日 | 2月8日（星期四）調整放假，於2月17日（星期六）補行上班，但不交易亦不交割。 | [來源](https://wwwc.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule?date=20240101&response=json) |
| 2024-02-09 | annual_holiday | 農曆除夕 | 依規定放假1日。 | [來源](https://wwwc.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule?date=20240101&response=json) |
| 2024-02-10 | annual_holiday | 農曆春節 | 依規定於2月10日至2月12日放假3日。<br><br>2月10日及2月11日適逢星期六及星期日，於2月13日（星期二）及2月14日（星期三）補假2日。<br> | [來源](https://wwwc.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule?date=20240101&response=json) |
| 2024-02-11 | annual_holiday | 農曆春節 | 依規定於2月10日至2月12日放假3日。  2月10日及2月11日適逢星期六及星期日，於2月13日（星期二）及2月14日（星期三）補假2日。 | [來源](https://wwwc.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule?date=20240101&response=json) |
| 2024-02-12 | annual_holiday | 農曆春節 | 依規定於2月10日至2月12日放假3日。<br><br>2月10日及2月11日適逢星期六及星期日，於2月13日（星期二）及2月14日（星期三）補假2日。<br> | [來源](https://wwwc.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule?date=20240101&response=json) |
| 2024-02-13 | annual_holiday | 農曆春節 | 依規定於2月10日至2月12日放假3日。<br><br>2月10日及2月11日適逢星期六及星期日，於2月13日（星期二）及2月14日（星期三）補假2日。<br> | [來源](https://wwwc.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule?date=20240101&response=json) |
| 2024-02-14 | annual_holiday | 農曆春節 | 依規定於2月10日至2月12日放假3日。<br><br>2月10日及2月11日適逢星期六及星期日，於2月13日（星期二）及2月14日（星期三）補假2日。<br> | [來源](https://wwwc.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule?date=20240101&response=json) |
| 2024-02-28 | annual_holiday | 和平紀念日 | 依規定放假1日。 | [來源](https://wwwc.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule?date=20240101&response=json) |
| 2024-04-04 | annual_holiday | 兒童節 | 依規定放假1日。 | [來源](https://wwwc.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule?date=20240101&response=json) |
| 2024-04-05 | annual_holiday | 民族掃墓節 | 依規定放假1日。 | [來源](https://wwwc.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule?date=20240101&response=json) |
| 2024-05-01 | annual_holiday | 勞動節 | 依規定放假1日。 | [來源](https://wwwc.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule?date=20240101&response=json) |
| 2024-06-10 | annual_holiday | 端午節 | 依規定放假1日。 | [來源](https://wwwc.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule?date=20240101&response=json) |
| 2024-07-24 | emergency_closure | 凱米颱風臨時休市 | 證交所 113 年 7 月 23 日新聞稿明列次日集中市場全日休市。 | [來源](https://investoredu.twse.com.tw/Mobile_pages/..%2FFileSystem%2FFileUpload%2F13b8d442-db49-4ad8-98a5-f454a285335c.pdf) |
| 2024-07-25 | emergency_closure | 凱米颱風臨時休市 | 證交所 113 年 7 月 24 日新聞稿明列次日集中市場全日休市。 | [來源](https://www.twse.com.tw/staticFiles/news/news/tsecnews/8a8216d6906d4d340190e4a4ef640174.pdf) |
| 2024-09-17 | annual_holiday | 中秋節 | 依規定放假1日。 | [來源](https://wwwc.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule?date=20240101&response=json) |
| 2024-10-02 | emergency_closure | 山陀兒颱風臨時休市 | 證交所 113 年 10 月 1 日新聞稿明列次日集中市場全日休市。 | [來源](https://www.twse.com.tw/staticFiles/news/news/tsecnews/8a8216d69236c2e301924806d5b4004e.pdf) |
| 2024-10-03 | emergency_closure | 山陀兒颱風臨時休市 | 證交所 113 年 10 月 2 日新聞稿明列次日集中市場全日休市。 | [來源](https://investoredu.twse.com.tw/Mobile_pages/..%2FFileSystem%2FFileUpload%2F2c6e50d4-a390-439c-a2c4-823937dee4b2.pdf) |
| 2024-10-10 | annual_holiday | 國慶日 | 依規定放假1日。 | [來源](https://wwwc.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule?date=20240101&response=json) |
| 2024-10-31 | emergency_closure | 康芮颱風臨時休市 | 證交所 113 年 10 月 30 日新聞稿明列次日集中市場全日休市。 | [來源](https://www.twse.com.tw/staticFiles/news/news/tsecnews/8a8216d69236c2e30192dd5179bc0327.pdf) |
| 2025-01-01 | annual_holiday | 中華民國開國紀念日 | 依規定放假1日。 | [來源](https://wwwc.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule?date=20250101&response=json) |
| 2025-01-23 | annual_holiday | 市場無交易，僅辦理結算交割作業 |  | [來源](https://wwwc.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule?date=20250101&response=json) |
| 2025-01-24 | annual_holiday | 市場無交易，僅辦理結算交割作業 |  | [來源](https://wwwc.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule?date=20250101&response=json) |
| 2025-01-27 | annual_holiday | 農曆除夕前一日 | 1月27日（星期一）調整放假，於2月8日（星期六）補行上班，但不交易亦不交割。 | [來源](https://wwwc.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule?date=20250101&response=json) |
| 2025-01-28 | annual_holiday | 農曆除夕 | 依規定放假1日。 | [來源](https://wwwc.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule?date=20250101&response=json) |
| 2025-01-29 | annual_holiday | 農曆春節 | 依規定於1月29日至1月31日放假3日。 | [來源](https://wwwc.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule?date=20250101&response=json) |
| 2025-01-30 | annual_holiday | 農曆春節 | 依規定於1月29日至1月31日放假3日。 | [來源](https://wwwc.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule?date=20250101&response=json) |
| 2025-01-31 | annual_holiday | 農曆春節 | 依規定於1月29日至1月31日放假3日。 | [來源](https://wwwc.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule?date=20250101&response=json) |
| 2025-02-28 | annual_holiday | 和平紀念日 | 依規定放假1日。 | [來源](https://wwwc.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule?date=20250101&response=json) |
| 2025-04-03 | annual_holiday | 兒童節及民族掃墓節 | 兒童節與民族掃墓節為4月4日（星期五），依規定於前一日4月3日（星期四）放假。 | [來源](https://wwwc.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule?date=20250101&response=json) |
| 2025-04-04 | annual_holiday | 兒童節及民族掃墓節 | 依規定放假1日。 | [來源](https://wwwc.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule?date=20250101&response=json) |
| 2025-05-01 | annual_holiday | 勞動節 | 依規定放假1日。 | [來源](https://wwwc.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule?date=20250101&response=json) |
| 2025-05-30 | annual_holiday | 端午節 | 端午節為5月31日適逢星期六，依規定於前一日5月30日（星期五）補假。 | [來源](https://wwwc.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule?date=20250101&response=json) |
| 2025-05-31 | annual_holiday | 端午節 | 依規定放假1日。 | [來源](https://wwwc.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule?date=20250101&response=json) |
| 2025-09-28 | annual_holiday | 孔子誕辰紀念日/ 教師節 | 依規定放假1日。 | [來源](https://wwwc.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule?date=20250101&response=json) |
| 2025-09-29 | annual_holiday | 孔子誕辰紀念日/ 教師節 | 孔子誕辰紀念日/教師節為9月28日適逢星期日，依規定於後一日9月29日（星期一）補假。 | [來源](https://wwwc.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule?date=20250101&response=json) |
| 2025-10-06 | annual_holiday | 中秋節 | 依規定放假1日。 | [來源](https://wwwc.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule?date=20250101&response=json) |
| 2025-10-10 | annual_holiday | 國慶日 | 依規定放假1日。 | [來源](https://wwwc.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule?date=20250101&response=json) |
| 2025-10-24 | annual_holiday | 臺灣光復暨金門古寧頭大捷紀念日 | 臺灣光復暨金門古寧頭大捷紀念日為10月25日適逢星期六，依規定於前一日10月24日（星期五）補假。 | [來源](https://wwwc.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule?date=20250101&response=json) |
| 2025-10-25 | annual_holiday | 臺灣光復暨金門古寧頭大捷紀念日 | 依規定放假1日。 | [來源](https://wwwc.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule?date=20250101&response=json) |
| 2025-12-25 | annual_holiday | 行憲紀念日 | 依規定放假1日。 | [來源](https://wwwc.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule?date=20250101&response=json) |
| 2026-01-01 | annual_holiday | 中華民國開國紀念日 | 依規定放假1日。 | [來源](https://wwwc.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule?date=20260101&response=json) |
| 2026-02-12 | annual_holiday | 市場無交易，僅辦理結算交割作業 |  | [來源](https://wwwc.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule?date=20260101&response=json) |
| 2026-02-13 | annual_holiday | 市場無交易，僅辦理結算交割作業 |  | [來源](https://wwwc.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule?date=20260101&response=json) |
| 2026-02-15 | annual_holiday | 農曆除夕及春節 | 依規定於2月15日至2月19日放假5日。2月15日適逢星期日，於2月20日（星期五）補假。 | [來源](https://wwwc.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule?date=20260101&response=json) |
| 2026-02-16 | annual_holiday | 農曆除夕及春節 | 依規定於2月15日至2月19日放假5日。2月15日適逢星期日，於2月20日（星期五）補假。 | [來源](https://wwwc.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule?date=20260101&response=json) |
| 2026-02-17 | annual_holiday | 農曆除夕及春節 | 依規定於2月15日至2月19日放假5日。2月15日適逢星期日，於2月20日（星期五）補假。 | [來源](https://wwwc.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule?date=20260101&response=json) |
| 2026-02-18 | annual_holiday | 農曆除夕及春節 | 依規定於2月15日至2月19日放假5日。2月15日適逢星期日，於2月20日（星期五）補假。 | [來源](https://wwwc.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule?date=20260101&response=json) |
| 2026-02-19 | annual_holiday | 農曆除夕及春節 | 依規定於2月15日至2月19日放假5日。2月15日適逢星期日，於2月20日（星期五）補假。 | [來源](https://wwwc.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule?date=20260101&response=json) |
| 2026-02-20 | annual_holiday | 農曆除夕及春節 | 依規定於2月15日至2月19日放假5日。2月15日適逢星期日，於2月20日（星期五）補假。 | [來源](https://wwwc.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule?date=20260101&response=json) |
| 2026-02-27 | annual_holiday | 和平紀念日 | 和平紀念日為2月28日適逢星期六，於2月27日（星期五）補假。 | [來源](https://wwwc.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule?date=20260101&response=json) |
| 2026-02-28 | annual_holiday | 和平紀念日 | 依規定放假1日。 | [來源](https://wwwc.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule?date=20260101&response=json) |
| 2026-04-03 | annual_holiday | 兒童節及民族掃墓節 | 兒童節為4月4日適逢星期六，於4月3日（星期五）補假。 | [來源](https://wwwc.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule?date=20260101&response=json) |
| 2026-04-04 | annual_holiday | 兒童節及民族掃墓節 | 依規定放假1日。 | [來源](https://wwwc.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule?date=20260101&response=json) |
| 2026-04-05 | annual_holiday | 兒童節及民族掃墓節 | 依規定放假1日。 | [來源](https://wwwc.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule?date=20260101&response=json) |
| 2026-04-06 | annual_holiday | 兒童節及民族掃墓節 | 民族掃墓節為4月5日適逢星期日，於4月6日（星期一）補假。 | [來源](https://wwwc.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule?date=20260101&response=json) |
| 2026-05-01 | annual_holiday | 勞動節 | 依規定放假1日。 | [來源](https://wwwc.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule?date=20260101&response=json) |
| 2026-06-19 | annual_holiday | 端午節 | 依規定放假1日。 | [來源](https://wwwc.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule?date=20260101&response=json) |
| 2026-07-10 | emergency_closure | 巴威颱風臨時休市 | 證券商公會承銷作業公告載明因集中市場休市而順延相關作業；來源是公會與承銷業者公告，非交易所原始休市公告，來源層級另標示。 | [來源](https://www.twsa.org.tw/F01/F011.html) |
| 2026-09-25 | annual_holiday | 中秋節 | 依規定放假1日。 | [來源](https://wwwc.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule?date=20260101&response=json) |
| 2026-09-28 | annual_holiday | 孔子誕辰紀念日/ 教師節 | 依規定放假1日。 | [來源](https://wwwc.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule?date=20260101&response=json) |
| 2026-10-09 | annual_holiday | 國慶日 | 國慶日為10月10日適逢星期六，於10月9日（星期五）補假。 | [來源](https://wwwc.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule?date=20260101&response=json) |
| 2026-10-10 | annual_holiday | 國慶日 | 依規定放假1日。 | [來源](https://wwwc.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule?date=20260101&response=json) |
| 2026-10-23 | emergency_closure | 薔琵颱風臨時休市 | 證交所官方資訊商店公告 115 年 10 月 23 日休市；本名單依使用者確認共同適用上市、上櫃、興櫃股票。非由臺北市停班停課直接推定。 | [來源](https://eshop.twse.com.tw/zh/home/index) |
| 2026-10-25 | annual_holiday | 臺灣光復暨金門古寧頭大捷紀念日 | 依規定放假1日。 | [來源](https://wwwc.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule?date=20260101&response=json) |
| 2026-10-26 | annual_holiday | 臺灣光復暨金門古寧頭大捷紀念日 | 臺灣光復暨金門古寧頭大捷紀念日為10月25日適逢星期日，於10月26日（星期一）補假。 | [來源](https://wwwc.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule?date=20260101&response=json) |

## 全部已收錄休市日期（含週末）

```text
2016-10-29
2016-10-30
2016-11-05
2016-11-06
2016-11-12
2016-11-13
2016-11-19
2016-11-20
2016-11-26
2016-11-27
2016-12-03
2016-12-04
2016-12-10
2016-12-11
2016-12-17
2016-12-18
2016-12-24
2016-12-25
2016-12-31
2017-01-01
2017-01-02
2017-01-07
2017-01-08
2017-01-14
2017-01-15
2017-01-21
2017-01-22
2017-01-25
2017-01-26
2017-01-27
2017-01-28
2017-01-29
2017-01-30
2017-01-31
2017-02-01
2017-02-04
2017-02-05
2017-02-11
2017-02-12
2017-02-19
2017-02-25
2017-02-26
2017-02-27
2017-02-28
2017-03-04
2017-03-05
2017-03-11
2017-03-12
2017-03-18
2017-03-19
2017-03-25
2017-03-26
2017-04-01
2017-04-02
2017-04-03
2017-04-04
2017-04-08
2017-04-09
2017-04-15
2017-04-16
2017-04-22
2017-04-23
2017-04-29
2017-04-30
2017-05-01
2017-05-06
2017-05-07
2017-05-13
2017-05-14
2017-05-20
2017-05-21
2017-05-27
2017-05-28
2017-05-29
2017-05-30
2017-06-04
2017-06-10
2017-06-11
2017-06-17
2017-06-18
2017-06-24
2017-06-25
2017-07-01
2017-07-02
2017-07-08
2017-07-09
2017-07-15
2017-07-16
2017-07-22
2017-07-23
2017-07-29
2017-07-30
2017-08-05
2017-08-06
2017-08-12
2017-08-13
2017-08-19
2017-08-20
2017-08-26
2017-08-27
2017-09-02
2017-09-03
2017-09-09
2017-09-10
2017-09-16
2017-09-17
2017-09-23
2017-09-24
2017-10-01
2017-10-04
2017-10-07
2017-10-08
2017-10-09
2017-10-10
2017-10-14
2017-10-15
2017-10-21
2017-10-22
2017-10-28
2017-10-29
2017-11-04
2017-11-05
2017-11-11
2017-11-12
2017-11-18
2017-11-19
2017-11-25
2017-11-26
2017-12-02
2017-12-03
2017-12-09
2017-12-10
2017-12-16
2017-12-17
2017-12-23
2017-12-24
2017-12-30
2017-12-31
2018-01-01
2018-01-06
2018-01-07
2018-01-13
2018-01-14
2018-01-20
2018-01-21
2018-01-27
2018-01-28
2018-02-03
2018-02-04
2018-02-10
2018-02-11
2018-02-13
2018-02-14
2018-02-15
2018-02-16
2018-02-17
2018-02-18
2018-02-19
2018-02-20
2018-02-24
2018-02-25
2018-02-28
2018-03-03
2018-03-04
2018-03-10
2018-03-11
2018-03-17
2018-03-18
2018-03-24
2018-03-25
2018-04-01
2018-04-04
2018-04-05
2018-04-06
2018-04-07
2018-04-08
2018-04-14
2018-04-15
2018-04-21
2018-04-22
2018-04-28
2018-04-29
2018-05-01
2018-05-05
2018-05-06
2018-05-12
2018-05-13
2018-05-19
2018-05-20
2018-05-26
2018-05-27
2018-06-02
2018-06-03
2018-06-09
2018-06-10
2018-06-16
2018-06-17
2018-06-18
2018-06-23
2018-06-24
2018-06-30
2018-07-01
2018-07-07
2018-07-08
2018-07-14
2018-07-15
2018-07-21
2018-07-22
2018-07-28
2018-07-29
2018-08-04
2018-08-05
2018-08-11
2018-08-12
2018-08-18
2018-08-19
2018-08-25
2018-08-26
2018-09-01
2018-09-02
2018-09-08
2018-09-09
2018-09-15
2018-09-16
2018-09-22
2018-09-23
2018-09-24
2018-09-29
2018-09-30
2018-10-06
2018-10-07
2018-10-10
2018-10-13
2018-10-14
2018-10-20
2018-10-21
2018-10-27
2018-10-28
2018-11-03
2018-11-04
2018-11-10
2018-11-11
2018-11-17
2018-11-18
2018-11-24
2018-11-25
2018-12-01
2018-12-02
2018-12-08
2018-12-09
2018-12-15
2018-12-16
2018-12-23
2018-12-29
2018-12-30
2018-12-31
2019-01-01
2019-01-05
2019-01-06
2019-01-12
2019-01-13
2019-01-19
2019-01-20
2019-01-26
2019-01-27
2019-01-31
2019-02-01
2019-02-02
2019-02-03
2019-02-04
2019-02-05
2019-02-06
2019-02-07
2019-02-08
2019-02-09
2019-02-10
2019-02-16
2019-02-17
2019-02-23
2019-02-24
2019-02-28
2019-03-01
2019-03-02
2019-03-03
2019-03-09
2019-03-10
2019-03-16
2019-03-17
2019-03-23
2019-03-24
2019-03-30
2019-03-31
2019-04-04
2019-04-05
2019-04-06
2019-04-07
2019-04-13
2019-04-14
2019-04-20
2019-04-21
2019-04-27
2019-04-28
2019-05-01
2019-05-04
2019-05-05
2019-05-11
2019-05-12
2019-05-18
2019-05-19
2019-05-25
2019-05-26
2019-06-01
2019-06-02
2019-06-07
2019-06-08
2019-06-09
2019-06-15
2019-06-16
2019-06-22
2019-06-23
2019-06-29
2019-06-30
2019-07-06
2019-07-07
2019-07-13
2019-07-14
2019-07-20
2019-07-21
2019-07-27
2019-07-28
2019-08-03
2019-08-04
2019-08-09
2019-08-10
2019-08-11
2019-08-17
2019-08-18
2019-08-24
2019-08-25
2019-08-31
2019-09-01
2019-09-07
2019-09-08
2019-09-13
2019-09-14
2019-09-15
2019-09-21
2019-09-22
2019-09-28
2019-09-29
2019-09-30
2019-10-05
2019-10-06
2019-10-10
2019-10-11
2019-10-12
2019-10-13
2019-10-19
2019-10-20
2019-10-26
2019-10-27
2019-11-02
2019-11-03
2019-11-09
2019-11-10
2019-11-16
2019-11-17
2019-11-23
2019-11-24
2019-11-30
2019-12-01
2019-12-07
2019-12-08
2019-12-14
2019-12-15
2019-12-21
2019-12-22
2019-12-28
2019-12-29
2020-01-01
2020-01-04
2020-01-05
2020-01-11
2020-01-12
2020-01-18
2020-01-19
2020-01-21
2020-01-22
2020-01-23
2020-01-24
2020-01-25
2020-01-26
2020-01-27
2020-01-28
2020-01-29
2020-02-01
2020-02-02
2020-02-08
2020-02-09
2020-02-15
2020-02-16
2020-02-22
2020-02-23
2020-02-28
2020-02-29
2020-03-01
2020-03-07
2020-03-08
2020-03-14
2020-03-15
2020-03-21
2020-03-22
2020-03-28
2020-03-29
2020-04-02
2020-04-03
2020-04-04
2020-04-05
2020-04-11
2020-04-12
2020-04-18
2020-04-19
2020-04-25
2020-04-26
2020-05-01
2020-05-02
2020-05-03
2020-05-09
2020-05-10
2020-05-16
2020-05-17
2020-05-23
2020-05-24
2020-05-30
2020-05-31
2020-06-06
2020-06-07
2020-06-13
2020-06-14
2020-06-20
2020-06-21
2020-06-25
2020-06-26
2020-06-27
2020-06-28
2020-07-04
2020-07-05
2020-07-11
2020-07-12
2020-07-18
2020-07-19
2020-07-25
2020-07-26
2020-08-01
2020-08-02
2020-08-08
2020-08-09
2020-08-15
2020-08-16
2020-08-22
2020-08-23
2020-08-29
2020-08-30
2020-09-05
2020-09-06
2020-09-12
2020-09-13
2020-09-19
2020-09-20
2020-09-26
2020-09-27
2020-10-01
2020-10-02
2020-10-03
2020-10-04
2020-10-09
2020-10-10
2020-10-11
2020-10-17
2020-10-18
2020-10-24
2020-10-25
2020-10-31
2020-11-01
2020-11-07
2020-11-08
2020-11-14
2020-11-15
2020-11-21
2020-11-22
2020-11-28
2020-11-29
2020-12-05
2020-12-06
2020-12-12
2020-12-13
2020-12-19
2020-12-20
2020-12-26
2020-12-27
2021-01-01
2021-01-02
2021-01-03
2021-01-09
2021-01-10
2021-01-16
2021-01-17
2021-01-23
2021-01-24
2021-01-30
2021-01-31
2021-02-06
2021-02-07
2021-02-08
2021-02-09
2021-02-10
2021-02-11
2021-02-12
2021-02-13
2021-02-14
2021-02-15
2021-02-16
2021-02-20
2021-02-21
2021-02-27
2021-02-28
2021-03-01
2021-03-06
2021-03-07
2021-03-13
2021-03-14
2021-03-20
2021-03-21
2021-03-27
2021-03-28
2021-04-02
2021-04-03
2021-04-04
2021-04-05
2021-04-10
2021-04-11
2021-04-17
2021-04-18
2021-04-24
2021-04-25
2021-04-30
2021-05-01
2021-05-02
2021-05-08
2021-05-09
2021-05-15
2021-05-16
2021-05-22
2021-05-23
2021-05-29
2021-05-30
2021-06-05
2021-06-06
2021-06-12
2021-06-13
2021-06-14
2021-06-19
2021-06-20
2021-06-26
2021-06-27
2021-07-03
2021-07-04
2021-07-10
2021-07-11
2021-07-17
2021-07-18
2021-07-24
2021-07-25
2021-07-31
2021-08-01
2021-08-07
2021-08-08
2021-08-14
2021-08-15
2021-08-21
2021-08-22
2021-08-28
2021-08-29
2021-09-04
2021-09-05
2021-09-11
2021-09-12
2021-09-18
2021-09-19
2021-09-20
2021-09-21
2021-09-25
2021-09-26
2021-10-02
2021-10-03
2021-10-09
2021-10-10
2021-10-11
2021-10-16
2021-10-17
2021-10-23
2021-10-24
2021-10-30
2021-10-31
2021-11-06
2021-11-07
2021-11-13
2021-11-14
2021-11-20
2021-11-21
2021-11-27
2021-11-28
2021-12-04
2021-12-05
2021-12-11
2021-12-12
2021-12-18
2021-12-19
2021-12-25
2021-12-26
2021-12-31
2022-01-01
2022-01-02
2022-01-08
2022-01-09
2022-01-15
2022-01-16
2022-01-22
2022-01-23
2022-01-27
2022-01-28
2022-01-29
2022-01-30
2022-01-31
2022-02-01
2022-02-02
2022-02-03
2022-02-04
2022-02-05
2022-02-06
2022-02-12
2022-02-13
2022-02-19
2022-02-20
2022-02-26
2022-02-27
2022-02-28
2022-03-05
2022-03-06
2022-03-12
2022-03-13
2022-03-19
2022-03-20
2022-03-26
2022-03-27
2022-04-02
2022-04-03
2022-04-04
2022-04-05
2022-04-09
2022-04-10
2022-04-16
2022-04-17
2022-04-23
2022-04-24
2022-04-30
2022-05-01
2022-05-02
2022-05-07
2022-05-08
2022-05-14
2022-05-15
2022-05-21
2022-05-22
2022-05-28
2022-05-29
2022-06-03
2022-06-04
2022-06-05
2022-06-11
2022-06-12
2022-06-18
2022-06-19
2022-06-25
2022-06-26
2022-07-02
2022-07-03
2022-07-09
2022-07-10
2022-07-16
2022-07-17
2022-07-23
2022-07-24
2022-07-30
2022-07-31
2022-08-06
2022-08-07
2022-08-13
2022-08-14
2022-08-20
2022-08-21
2022-08-27
2022-08-28
2022-09-03
2022-09-04
2022-09-09
2022-09-10
2022-09-11
2022-09-17
2022-09-18
2022-09-24
2022-09-25
2022-10-01
2022-10-02
2022-10-08
2022-10-09
2022-10-10
2022-10-15
2022-10-16
2022-10-22
2022-10-23
2022-10-29
2022-10-30
2022-11-05
2022-11-06
2022-11-12
2022-11-13
2022-11-19
2022-11-20
2022-11-26
2022-11-27
2022-12-03
2022-12-04
2022-12-10
2022-12-11
2022-12-17
2022-12-18
2022-12-24
2022-12-25
2022-12-31
2023-01-01
2023-01-02
2023-01-07
2023-01-08
2023-01-14
2023-01-15
2023-01-18
2023-01-19
2023-01-20
2023-01-21
2023-01-22
2023-01-23
2023-01-24
2023-01-25
2023-01-26
2023-01-27
2023-01-28
2023-01-29
2023-02-04
2023-02-05
2023-02-11
2023-02-12
2023-02-18
2023-02-19
2023-02-25
2023-02-26
2023-02-27
2023-02-28
2023-03-04
2023-03-05
2023-03-11
2023-03-12
2023-03-18
2023-03-19
2023-03-25
2023-03-26
2023-04-01
2023-04-02
2023-04-03
2023-04-04
2023-04-05
2023-04-08
2023-04-09
2023-04-15
2023-04-16
2023-04-22
2023-04-23
2023-04-29
2023-04-30
2023-05-01
2023-05-06
2023-05-07
2023-05-13
2023-05-14
2023-05-20
2023-05-21
2023-05-27
2023-05-28
2023-06-03
2023-06-04
2023-06-10
2023-06-11
2023-06-17
2023-06-18
2023-06-22
2023-06-23
2023-06-24
2023-06-25
2023-07-01
2023-07-02
2023-07-08
2023-07-09
2023-07-15
2023-07-16
2023-07-22
2023-07-23
2023-07-29
2023-07-30
2023-08-03
2023-08-05
2023-08-06
2023-08-12
2023-08-13
2023-08-19
2023-08-20
2023-08-26
2023-08-27
2023-09-02
2023-09-03
2023-09-09
2023-09-10
2023-09-16
2023-09-17
2023-09-23
2023-09-24
2023-09-29
2023-09-30
2023-10-01
2023-10-07
2023-10-08
2023-10-09
2023-10-10
2023-10-14
2023-10-15
2023-10-21
2023-10-22
2023-10-28
2023-10-29
2023-11-04
2023-11-05
2023-11-11
2023-11-12
2023-11-18
2023-11-19
2023-11-25
2023-11-26
2023-12-02
2023-12-03
2023-12-09
2023-12-10
2023-12-16
2023-12-17
2023-12-23
2023-12-24
2023-12-30
2023-12-31
2024-01-01
2024-01-06
2024-01-07
2024-01-13
2024-01-14
2024-01-20
2024-01-21
2024-01-27
2024-01-28
2024-02-03
2024-02-04
2024-02-06
2024-02-07
2024-02-08
2024-02-09
2024-02-10
2024-02-11
2024-02-12
2024-02-13
2024-02-14
2024-02-17
2024-02-18
2024-02-24
2024-02-25
2024-02-28
2024-03-02
2024-03-03
2024-03-09
2024-03-10
2024-03-16
2024-03-17
2024-03-23
2024-03-24
2024-03-30
2024-03-31
2024-04-04
2024-04-05
2024-04-06
2024-04-07
2024-04-13
2024-04-14
2024-04-20
2024-04-21
2024-04-27
2024-04-28
2024-05-01
2024-05-04
2024-05-05
2024-05-11
2024-05-12
2024-05-18
2024-05-19
2024-05-25
2024-05-26
2024-06-01
2024-06-02
2024-06-08
2024-06-09
2024-06-10
2024-06-15
2024-06-16
2024-06-22
2024-06-23
2024-06-29
2024-06-30
2024-07-06
2024-07-07
2024-07-13
2024-07-14
2024-07-20
2024-07-21
2024-07-24
2024-07-25
2024-07-27
2024-07-28
2024-08-03
2024-08-04
2024-08-10
2024-08-11
2024-08-17
2024-08-18
2024-08-24
2024-08-25
2024-08-31
2024-09-01
2024-09-07
2024-09-08
2024-09-14
2024-09-15
2024-09-17
2024-09-21
2024-09-22
2024-09-28
2024-09-29
2024-10-02
2024-10-03
2024-10-05
2024-10-06
2024-10-10
2024-10-12
2024-10-13
2024-10-19
2024-10-20
2024-10-26
2024-10-27
2024-10-31
2024-11-02
2024-11-03
2024-11-09
2024-11-10
2024-11-16
2024-11-17
2024-11-23
2024-11-24
2024-11-30
2024-12-01
2024-12-07
2024-12-08
2024-12-14
2024-12-15
2024-12-21
2024-12-22
2024-12-28
2024-12-29
2025-01-01
2025-01-04
2025-01-05
2025-01-11
2025-01-12
2025-01-18
2025-01-19
2025-01-23
2025-01-24
2025-01-25
2025-01-26
2025-01-27
2025-01-28
2025-01-29
2025-01-30
2025-01-31
2025-02-01
2025-02-02
2025-02-08
2025-02-09
2025-02-15
2025-02-16
2025-02-22
2025-02-23
2025-02-28
2025-03-01
2025-03-02
2025-03-08
2025-03-09
2025-03-15
2025-03-16
2025-03-22
2025-03-23
2025-03-29
2025-03-30
2025-04-03
2025-04-04
2025-04-05
2025-04-06
2025-04-12
2025-04-13
2025-04-19
2025-04-20
2025-04-26
2025-04-27
2025-05-01
2025-05-03
2025-05-04
2025-05-10
2025-05-11
2025-05-17
2025-05-18
2025-05-24
2025-05-25
2025-05-30
2025-05-31
2025-06-01
2025-06-07
2025-06-08
2025-06-14
2025-06-15
2025-06-21
2025-06-22
2025-06-28
2025-06-29
2025-07-05
2025-07-06
2025-07-12
2025-07-13
2025-07-19
2025-07-20
2025-07-26
2025-07-27
2025-08-02
2025-08-03
2025-08-09
2025-08-10
2025-08-16
2025-08-17
2025-08-23
2025-08-24
2025-08-30
2025-08-31
2025-09-06
2025-09-07
2025-09-13
2025-09-14
2025-09-20
2025-09-21
2025-09-27
2025-09-28
2025-09-29
2025-10-04
2025-10-05
2025-10-06
2025-10-10
2025-10-11
2025-10-12
2025-10-18
2025-10-19
2025-10-24
2025-10-25
2025-10-26
2025-11-01
2025-11-02
2025-11-08
2025-11-09
2025-11-15
2025-11-16
2025-11-22
2025-11-23
2025-11-29
2025-11-30
2025-12-06
2025-12-07
2025-12-13
2025-12-14
2025-12-20
2025-12-21
2025-12-25
2025-12-27
2025-12-28
2026-01-01
2026-01-03
2026-01-04
2026-01-10
2026-01-11
2026-01-17
2026-01-18
2026-01-24
2026-01-25
2026-01-31
2026-02-01
2026-02-07
2026-02-08
2026-02-12
2026-02-13
2026-02-14
2026-02-15
2026-02-16
2026-02-17
2026-02-18
2026-02-19
2026-02-20
2026-02-21
2026-02-22
2026-02-27
2026-02-28
2026-03-01
2026-03-07
2026-03-08
2026-03-14
2026-03-15
2026-03-21
2026-03-22
2026-03-28
2026-03-29
2026-04-03
2026-04-04
2026-04-05
2026-04-06
2026-04-11
2026-04-12
2026-04-18
2026-04-19
2026-04-25
2026-04-26
2026-05-01
2026-05-02
2026-05-03
2026-05-09
2026-05-10
2026-05-16
2026-05-17
2026-05-23
2026-05-24
2026-05-30
2026-05-31
2026-06-06
2026-06-07
2026-06-13
2026-06-14
2026-06-19
2026-06-20
2026-06-21
2026-06-27
2026-06-28
2026-07-04
2026-07-05
2026-07-10
2026-07-11
2026-07-12
2026-07-18
2026-07-19
2026-07-25
2026-07-26
2026-08-01
2026-08-02
2026-08-08
2026-08-09
2026-08-15
2026-08-16
2026-08-22
2026-08-23
2026-08-29
2026-08-30
2026-09-05
2026-09-06
2026-09-12
2026-09-13
2026-09-19
2026-09-20
2026-09-25
2026-09-26
2026-09-27
2026-09-28
2026-10-03
2026-10-04
2026-10-09
2026-10-10
2026-10-11
2026-10-17
2026-10-18
2026-10-23
2026-10-24
2026-10-25
2026-10-26
```

## 待分類紀錄

無未分類紀錄；不代表缺漏年度已補齊或臨時休市已全部收錄。
