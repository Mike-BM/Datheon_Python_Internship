#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import math
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
COMPANIES_FILE = BASE_DIR / "data" / "sp500_companies.csv"
LOG_FILE = BASE_DIR / "outputs" / "portfolio_log.csv"
CHART_DIR = BASE_DIR / "outputs"
DEFAULT_MAX_WEIGHT = 40.0

LOG_FIELDS = [
    "timestamp",
    "record_type",
    "ticker",
    "company",
    "sector",
    "shares",
    "buy_price",
    "current_price",
    "cost_basis",
    "market_value",
    "gain_loss",
    "gain_loss_pct",
    "weight_pct",
    "price_source",
]


class LogFormatError(Exception):
    pass


@dataclass
class Holding:
    ticker: str
    buy_price: float
    shares: float
    current_price: float = 0.0
    price_source: str = ""
    company: str = ""
    sector: str = ""
    weight_pct: float = 0.0

    @property
    def cost_basis(self) -> float:
        return self.buy_price * self.shares

    @property
    def market_value(self) -> float:
        return self.current_price * self.shares

    @property
    def gain_loss(self) -> float:
        return self.market_value - self.cost_basis

    @property
    def gain_loss_pct(self) -> float:
        return (self.current_price - self.buy_price) / self.buy_price * 100


def is_valid_number(value: float) -> bool:
    return math.isfinite(value) and value > 0


def yahoo_symbol(ticker: str) -> str:
    return ticker.replace(".", "-")


def load_companies(path) -> dict:
    companies = {}
    try:
        with open(path, newline="", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                companies[row["Symbol"].strip().upper()] = (
                    row["Security"].strip(),
                    row["GICS Sector"].strip(),
                )
    except FileNotFoundError:
        print(f"Warning: {path} not found. Tickers will not be validated.")
    return companies


def get_live_price(ticker: str):
    try:
        import yfinance as yf
    except ImportError:
        return None
    try:
        stock = yf.Ticker(yahoo_symbol(ticker))
        price = stock.fast_info.get("last_price")
        if price and price > 0:
            return float(price)
        history = stock.history(period="1d")
        if not history.empty:
            return float(history["Close"].iloc[-1])
    except Exception:
        pass
    return None


def read_prices_file(path) -> dict:
    prices = {}
    with open(path, newline="", encoding="utf-8-sig") as f:
        for line_no, row in enumerate(csv.DictReader(f), start=2):
            try:
                price = float(row["price"])
                if not is_valid_number(price):
                    raise ValueError
                prices[row["ticker"].strip().upper()] = price
            except (KeyError, ValueError, AttributeError):
                print(f"  Skipping invalid price row {line_no} in {path}.")
    return prices


def ask_positive_number(prompt: str) -> float:
    while True:
        raw = input(prompt).strip().replace(",", "").replace("$", "")
        try:
            value = float(raw)
        except ValueError:
            print("  Please enter a valid number.")
            continue
        if is_valid_number(value):
            return value
        print("  Value must be greater than zero.")


def ask_yes_no(prompt: str) -> bool:
    return input(prompt).strip().lower() in ("y", "yes")


def collect_holdings_interactively(companies: dict) -> list:
    holdings = []
    print("\nEnter your holdings (press Enter with a blank ticker when finished):\n")
    while True:
        ticker = input("Ticker symbol: ").strip().upper()
        if not ticker:
            break
        if companies and ticker not in companies:
            print(f"  '{ticker}' was not found in the S&P 500 list.")
            if not ask_yes_no("  Keep this ticker anyway? (y/n): "):
                continue
        buy_price = ask_positive_number("  Buy price per share: ")
        shares = ask_positive_number("  Number of shares: ")
        holdings.append(Holding(ticker, buy_price, shares))
        print(f"  Added {shares:g} x {ticker} @ {buy_price:,.2f}\n")
    return holdings


def read_holdings_file(path) -> list:
    holdings = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        for line_no, row in enumerate(csv.DictReader(f), start=2):
            try:
                ticker = row["ticker"].strip().upper()
                buy_price = float(row["buy_price"])
                shares = float(row["shares"])
                if not ticker or not is_valid_number(buy_price) or not is_valid_number(shares):
                    raise ValueError
                holdings.append(Holding(ticker, buy_price, shares))
            except (KeyError, ValueError, AttributeError):
                print(f"  Skipping invalid row {line_no} in {path}.")
    return holdings


def merge_lots(holdings: list) -> list:
    merged: dict[str, Holding] = {}
    for h in holdings:
        if h.ticker not in merged:
            merged[h.ticker] = Holding(h.ticker, h.buy_price, h.shares)
            continue
        m = merged[h.ticker]
        total_cost = m.cost_basis + h.cost_basis
        m.shares += h.shares
        m.buy_price = total_cost / m.shares
    return list(merged.values())


def enrich_and_price(holdings: list, companies: dict, offline: bool = False,
                     price_map: dict | None = None) -> None:
    for h in holdings:
        h.company, h.sector = companies.get(h.ticker, ("", ""))

        if price_map and h.ticker in price_map:
            h.current_price, h.price_source = price_map[h.ticker], "file"
            continue

        price = None if offline else get_live_price(h.ticker)
        if price is not None:
            h.current_price, h.price_source = price, "live"
        else:
            if not offline:
                print(f"Could not fetch live price for {h.ticker}.")
            h.current_price = ask_positive_number(
                f"  Enter current price for {h.ticker}: "
            )
            h.price_source = "manual"


def portfolio_totals(holdings: list) -> dict:
    cost = sum(h.cost_basis for h in holdings)
    value = sum(h.market_value for h in holdings)
    gain = value - cost
    pct = (gain / cost * 100) if cost else 0.0
    return {"cost": cost, "value": value, "gain": gain, "pct": pct}


def assign_weights(holdings: list, totals: dict) -> None:
    for h in holdings:
        h.weight_pct = (h.market_value / totals["value"] * 100) if totals["value"] else 0.0


def sector_breakdown(holdings: list) -> list:
    buckets: dict[str, tuple[float, float]] = {}
    for h in holdings:
        key = h.sector or "Unclassified"
        value, gain = buckets.get(key, (0.0, 0.0))
        buckets[key] = (value + h.market_value, gain + h.gain_loss)
    total = sum(v for v, _ in buckets.values())
    rows = [(s, v, (v / total * 100) if total else 0.0, g) for s, (v, g) in buckets.items()]
    return sorted(rows, key=lambda r: r[1], reverse=True)


def concentration_warnings(holdings: list, max_weight: float = DEFAULT_MAX_WEIGHT) -> list:
    return [
        f"{h.ticker} is {h.weight_pct:.1f}% of the portfolio (limit {max_weight:g}%)."
        for h in holdings if h.weight_pct > max_weight
    ]


def print_report(holdings: list, totals: dict, sectors: list, warnings: list) -> None:
    header = (
        f"{'Ticker':<7}{'Company':<27}{'Shares':>9}{'Buy':>10}{'Now':>10}"
        f"{'Cost':>13}{'Value':>13}{'P/L $':>12}{'P/L %':>9}{'Weight':>9}"
    )
    line = "-" * len(header)
    print("\n" + line)
    print(header)
    print(line)
    for h in holdings:
        print(
            f"{h.ticker:<7}{h.company[:26]:<27}{h.shares:>9g}"
            f"{h.buy_price:>10,.2f}{h.current_price:>10,.2f}"
            f"{h.cost_basis:>13,.2f}{h.market_value:>13,.2f}"
            f"{h.gain_loss:>+12,.2f}{h.gain_loss_pct:>+8.2f}%{h.weight_pct:>8.1f}%"
        )
    print(line)
    print(
        f"{'TOTAL':<7}{'':<27}{'':>9}{'':>10}{'':>10}"
        f"{totals['cost']:>13,.2f}{totals['value']:>13,.2f}"
        f"{totals['gain']:>+12,.2f}{totals['pct']:>+8.2f}%{100:>8.1f}%"
    )
    print(line)

    best = max(holdings, key=lambda h: h.gain_loss_pct)
    worst = min(holdings, key=lambda h: h.gain_loss_pct)
    print(f"Best performer : {best.ticker} ({best.gain_loss_pct:+.2f}%)")
    print(f"Worst performer: {worst.ticker} ({worst.gain_loss_pct:+.2f}%)")

    print("\nSector allocation")
    print(f"{'Sector':<28}{'Value':>13}{'Weight':>9}{'P/L $':>12}")
    for sector, value, weight, gain in sectors:
        print(f"{sector:<28}{value:>13,.2f}{weight:>8.1f}%{gain:>+12,.2f}")

    if warnings:
        print("\nConcentration warnings")
        for message in warnings:
            print(f"  ! {message}")


def _log_header_ok(path: Path) -> bool:
    if not path.exists() or path.stat().st_size == 0:
        return True
    with open(path, newline="", encoding="utf-8") as f:
        return next(csv.reader(f), []) == LOG_FIELDS


def append_to_log(holdings: list, totals: dict, log_path) -> None:
    path = Path(log_path)
    if not _log_header_ok(path):
        raise LogFormatError(
            f"{path} has a different column layout. Use --log with a new file name."
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().isoformat(timespec="seconds")
    write_header = not path.exists() or path.stat().st_size == 0

    with open(path, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=LOG_FIELDS)
        if write_header:
            writer.writeheader()
        for h in holdings:
            writer.writerow({
                "timestamp": timestamp,
                "record_type": "HOLDING",
                "ticker": h.ticker,
                "company": h.company,
                "sector": h.sector,
                "shares": h.shares,
                "buy_price": round(h.buy_price, 2),
                "current_price": round(h.current_price, 2),
                "cost_basis": round(h.cost_basis, 2),
                "market_value": round(h.market_value, 2),
                "gain_loss": round(h.gain_loss, 2),
                "gain_loss_pct": round(h.gain_loss_pct, 2),
                "weight_pct": round(h.weight_pct, 2),
                "price_source": h.price_source,
            })
        writer.writerow({
            "timestamp": timestamp,
            "record_type": "TOTAL",
            "ticker": "PORTFOLIO",
            "cost_basis": round(totals["cost"], 2),
            "market_value": round(totals["value"], 2),
            "gain_loss": round(totals["gain"], 2),
            "gain_loss_pct": round(totals["pct"], 2),
            "weight_pct": 100.0,
        })
    print(f"\nSummary logged to {path}")


def show_history(log_path) -> None:
    try:
        with open(log_path, newline="", encoding="utf-8") as f:
            runs = [r for r in csv.DictReader(f) if r["record_type"] == "TOTAL"]
    except FileNotFoundError:
        print(f"No log found at {log_path} yet. Run the tracker first.")
        return
    if not runs:
        print("The log has no completed runs yet.")
        return

    print(f"\n{'Timestamp':<21}{'Cost':>14}{'Value':>14}{'P/L $':>13}{'P/L %':>9}")
    print("-" * 71)
    for r in runs:
        print(
            f"{r['timestamp']:<21}{float(r['cost_basis']):>14,.2f}"
            f"{float(r['market_value']):>14,.2f}{float(r['gain_loss']):>+13,.2f}"
            f"{float(r['gain_loss_pct']):>+8.2f}%"
        )

    if len(runs) > 1:
        prev, last = runs[-2], runs[-1]
        if abs(float(prev["cost_basis"]) - float(last["cost_basis"])) < 0.005:
            change = float(last["market_value"]) - float(prev["market_value"])
            print(f"\nChange in value since the previous run: {change:+,.2f}")


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="Stock Portfolio Performance Tracker",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--file", help="CSV of holdings (ticker,buy_price,shares)")
    p.add_argument("--prices", help="CSV of current prices (ticker,price)")
    p.add_argument("--offline", action="store_true",
                   help="skip live price lookup; unlisted prices are typed in")
    p.add_argument("--history", action="store_true",
                   help="show past portfolio totals from the log and exit")
    p.add_argument("--charts", action="store_true",
                   help="save charts as PNG files (requires matplotlib)")
    p.add_argument("--max-weight", type=float, default=DEFAULT_MAX_WEIGHT,
                   help="warn when one position exceeds this %% of the portfolio")
    p.add_argument("--no-log", action="store_true", help="do not write to the log")
    p.add_argument("--log", default=str(LOG_FILE), help="path to CSV log file")
    p.add_argument("--chart-dir", default=str(CHART_DIR), help="folder for generated charts")
    p.add_argument("--companies", default=str(COMPANIES_FILE),
                   help="path to S&P 500 reference CSV")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)

    if args.history:
        show_history(args.log)
        return 0

    companies = load_companies(args.companies)

    try:
        holdings = (read_holdings_file(args.file) if args.file
                    else collect_holdings_interactively(companies))
        if not holdings:
            print("No holdings entered. Nothing to calculate.")
            return 0
        holdings = merge_lots(holdings)
        price_map = read_prices_file(args.prices) if args.prices else None
        enrich_and_price(holdings, companies, args.offline, price_map)
    except FileNotFoundError as err:
        print(f"File not found: {err.filename}", file=sys.stderr)
        return 1
    except (EOFError, KeyboardInterrupt):
        print("\nInput cancelled.", file=sys.stderr)
        return 1

    totals = portfolio_totals(holdings)
    assign_weights(holdings, totals)
    sectors = sector_breakdown(holdings)
    warnings = concentration_warnings(holdings, args.max_weight)
    print_report(holdings, totals, sectors, warnings)

    if not args.no_log:
        try:
            append_to_log(holdings, totals, args.log)
        except LogFormatError as err:
            print(f"\n{err}", file=sys.stderr)
            return 1
        except PermissionError:
            print(f"\nCould not write to {args.log}. Please close the file if open "
                  "in another program and try again.", file=sys.stderr)
            return 1

    if args.charts:
        try:
            from charts import make_charts
            for path in make_charts(holdings, totals, sectors, args.chart_dir):
                print(f"Chart saved to {path}")
        except ImportError:
            print("\nCharts require matplotlib: pip install matplotlib")

    return 0


if __name__ == "__main__":
    sys.exit(main())
