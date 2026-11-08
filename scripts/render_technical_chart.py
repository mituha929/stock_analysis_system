r"""Read MySQL K-lines and render the V2 indicators to a PNG image.

Run from the ``capital_api`` directory::

    .\.venv\Scripts\python.exe .\scripts\render_technical_chart.py --symbol 2330
    .\.venv\Scripts\python.exe .\scripts\render_technical_chart.py --symbol 2330 --bars 360
    .\.venv\Scripts\python.exe .\scripts\render_technical_chart.py --symbol 2330 --table odd_lot_candle

This is read-only: it does not call the Capital DLL or write to MySQL.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from dotenv import load_dotenv


INTERVALS = ("1m", "5m", "15m", "60m", "day", "week", "month")
TABLES = ("candles", "odd_lot_candle")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="將 MySQL K 線與技術指標輸出為 PNG 圖片")
    parser.add_argument("--symbol", required=True, help="股票代號，例如 2330")
    parser.add_argument("--interval", choices=INTERVALS, default="day", help="K 線週期；預設 day")
    parser.add_argument("--table", choices=TABLES, default="candles", help="candles=整張；odd_lot_candle=零股")
    parser.add_argument("--bars", type=int, default=180, help="圖上顯示的最近 K 線根數；預設 180")
    parser.add_argument("--output", type=Path, default=None, help="PNG 輸出路徑；未指定時寫入 output/charts")
    parser.add_argument("--dpi", type=int, default=150, help="圖片解析度；預設 150")
    return parser


def _tail(values: list[float | None], start: int) -> list[float | None]:
    return values[start:]


def _configure_font(plt) -> None:
    plt.rcParams["font.sans-serif"] = ["Microsoft JhengHei", "Noto Sans CJK TC", "Arial Unicode MS", "DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False


def render_chart(candles, indicators: dict, symbol: str, interval: str, output_path: Path, dpi: int) -> None:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.dates as mdates
        import matplotlib.pyplot as plt
    except ImportError as error:
        raise RuntimeError("尚未安裝 matplotlib；請執行 pip install -r requirements.txt") from error

    _configure_font(plt)
    dates = [item.timestamp for item in candles]
    opens, highs, lows, closes = ([getattr(item, field) for item in candles] for field in ("open", "high", "low", "close"))
    volumes = [item.volume for item in candles]
    x_values = mdates.date2num(dates)
    width = max((x_values[-1] - x_values[0]) / max(len(candles), 1) * 0.65, 0.005)
    colors = ["#d62728" if close >= opening else "#2ca02c" for opening, close in zip(opens, closes)]

    figure, axes = plt.subplots(
        6, 1, sharex=True, figsize=(16, 18),
        gridspec_kw={"height_ratios": [3.4, 1.1, 1.6, 1.35, 1.35, 1.35]}, layout="constrained",
    )
    price_axis, volume_axis, macd_axis, kd_axis, rsi_axis, dmi_axis = axes

    for x, opening, high, low, close, color in zip(x_values, opens, highs, lows, closes, colors):
        price_axis.vlines(x, low, high, color=color, linewidth=0.8)
        body_height = max(abs(close - opening), max(high - low, 1.0) * 0.001)
        price_axis.bar(x, body_height, bottom=min(opening, close), width=width, color=color, edgecolor=color)
    for key, label in (("ma5", "MA5"), ("ma10", "MA10"), ("ma20", "MA20"), ("ma60", "MA60")):
        price_axis.plot(dates, indicators["ma"][key], label=label, linewidth=1.0)
    bands = indicators["bollinger"]
    for key, style, label in (("upper", "--", "布林上軌"), ("middle", ":", "布林中線"), ("lower", "--", "布林下軌")):
        price_axis.plot(dates, bands[key], color="#777777", linestyle=style, linewidth=0.8, label=label)
    price_axis.set_title(f"{symbol}｜{interval} K｜技術分析")
    price_axis.set_ylabel("價格")
    price_axis.legend(loc="upper left", ncol=4, fontsize=8)

    volume_axis.bar(dates, volumes, color=colors, width=width)
    volume_axis.plot(dates, indicators["volume"]["volume_ma5"], label="量 MA5", linewidth=1.0)
    volume_axis.plot(dates, indicators["volume"]["volume_ma20"], label="量 MA20", linewidth=1.0)
    volume_axis.set_ylabel("成交量")
    volume_axis.legend(loc="upper left", fontsize=8)

    macd = indicators["macd"]
    histogram = macd["histogram"]
    bar_colors = ["#d62728" if value is not None and value >= 0 else "#2ca02c" for value in histogram]
    macd_axis.bar(dates, [value or 0 for value in histogram], color=bar_colors, width=width, label="柱狀體")
    macd_axis.plot(dates, macd["dif"], label="DIF", linewidth=1.0)
    macd_axis.plot(dates, macd["signal"], label="MACD", linewidth=1.0)
    macd_axis.axhline(0, color="#666666", linewidth=0.7)
    macd_axis.set_ylabel("MACD")
    macd_axis.legend(loc="upper left", fontsize=8)

    kd_axis.plot(dates, indicators["kd"]["k"], label="K", linewidth=1.0)
    kd_axis.plot(dates, indicators["kd"]["d"], label="D", linewidth=1.0)
    kd_axis.axhline(80, color="#888888", linestyle="--", linewidth=0.7)
    kd_axis.axhline(20, color="#888888", linestyle="--", linewidth=0.7)
    kd_axis.set_ylim(0, 100)
    kd_axis.set_ylabel("KD")
    kd_axis.legend(loc="upper left", fontsize=8)

    rsi_axis.plot(dates, indicators["rsi"]["rsi14"], label="RSI14", linewidth=1.0)
    rsi_axis.axhline(70, color="#888888", linestyle="--", linewidth=0.7)
    rsi_axis.axhline(30, color="#888888", linestyle="--", linewidth=0.7)
    rsi_axis.set_ylim(0, 100)
    rsi_axis.set_ylabel("RSI")
    rsi_axis.legend(loc="upper left", fontsize=8)

    dmi = indicators["dmi_adx"]
    for key, label in (("plus_di", "+DI"), ("minus_di", "-DI"), ("adx", "ADX")):
        dmi_axis.plot(dates, dmi[key], label=label, linewidth=1.0)
    dmi_axis.axhline(25, color="#888888", linestyle="--", linewidth=0.7)
    dmi_axis.set_ylabel("DMI/ADX")
    dmi_axis.legend(loc="upper left", fontsize=8)

    for axis in axes:
        axis.grid(alpha=0.2)
    locator = mdates.AutoDateLocator()
    dmi_axis.xaxis.set_major_locator(locator)
    dmi_axis.xaxis.set_major_formatter(mdates.ConciseDateFormatter(locator))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    figure.savefig(output_path, dpi=dpi, bbox_inches="tight")
    plt.close(figure)


def main() -> int:
    arguments = build_parser().parse_args()
    if arguments.bars < 2 or arguments.dpi < 72:
        print("--bars 至少為 2，--dpi 至少為 72", file=sys.stderr)
        return 2
    project_root = Path(__file__).resolve().parents[1]
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))
    env_path = project_root / ".env"
    if not env_path.is_file():
        print(f"找不到 .env：{env_path}", file=sys.stderr)
        return 2
    load_dotenv(env_path)
    try:
        from app.indicators import calculate_indicators
        from app.services.mysql_instrument_repository import MySQLCandleRepository, MySQLRepositoryError
        candles = MySQLCandleRepository().get_candles(
            arguments.symbol.upper(), arguments.interval, arguments.table, limit=max(arguments.bars, 360),
        )
        if len(candles) < 2:
            print(f"找不到 {arguments.symbol.upper()} 的 {arguments.interval} K 線資料", file=sys.stderr)
            return 1
        indicators = calculate_indicators(candles)
    except MySQLRepositoryError as error:
        print(f"讀取 MySQL K 線失敗：{error}", file=sys.stderr)
        return 1
    except ValueError as error:
        print(f"K 線資料格式異常：{error}", file=sys.stderr)
        return 1

    display_candles = candles[-arguments.bars:]
    start = len(candles) - len(display_candles)
    display_indicators = {
        group: {key: _tail(values, start) for key, values in data.items()}
        for group, data in indicators.items() if group != "candles"
    }
    output_path = arguments.output or project_root / "output" / "charts" / f"{arguments.symbol.upper()}_{arguments.interval}_{arguments.table}.png"
    try:
        render_chart(display_candles, display_indicators, arguments.symbol.upper(), arguments.interval, output_path, arguments.dpi)
    except RuntimeError as error:
        print(error, file=sys.stderr)
        return 2
    print(f"已輸出圖片：{output_path.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
