import csv
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import portfolio_tracker as pt

DATA = ROOT / "data"
GICS_SECTORS = {
    "Communication Services",
    "Consumer Discretionary",
    "Consumer Staples",
    "Energy",
    "Financials",
    "Health Care",
    "Industrials",
    "Information Technology",
    "Materials",
    "Real Estate",
    "Utilities",
}


def priced(ticker, buy, shares, now, sector=""):
    return pt.Holding(ticker, buy, shares, current_price=now, sector=sector)


def test_holding_math():
    h = priced("AAPL", 150, 10, 190)
    assert h.cost_basis == 1500
    assert h.market_value == 1900
    assert h.gain_loss == 400
    assert h.gain_loss_pct == pytest.approx(26.6667, abs=1e-3)


def test_loss_is_negative():
    h = priced("PFE", 38, 40, 27)
    assert h.gain_loss == -440
    assert h.gain_loss_pct == pytest.approx(-28.947, abs=1e-3)


def test_portfolio_totals():
    totals = pt.portfolio_totals([priced("A", 100, 10, 110), priced("B", 50, 20, 40)])
    assert totals["cost"] == 2000
    assert totals["value"] == 1900
    assert totals["gain"] == -100
    assert totals["pct"] == pytest.approx(-5.0)


def test_weights_sum_to_100():
    holdings = [priced("A", 10, 10, 10), priced("B", 10, 10, 30), priced("C", 10, 5, 20)]
    pt.assign_weights(holdings, pt.portfolio_totals(holdings))
    assert sum(h.weight_pct for h in holdings) == pytest.approx(100)


@pytest.mark.parametrize("value, ok", [
    (1, True),
    (0.01, True),
    (0, False),
    (-5, False),
    (float("nan"), False),
    (float("inf"), False),
])
def test_is_valid_number(value, ok):
    assert pt.is_valid_number(value) is ok


def test_read_holdings_skips_bad_rows(tmp_path, capsys):
    f = tmp_path / "h.csv"
    f.write_text(
        "ticker,buy_price,shares\n"
        "aapl,150,10\n"
        "MSFT,-1,5\n"
        "NVDA,95.5,0\n"
        "TSLA,abc,3\n"
        ",10,10\n"
        "JPM,140,6\n"
    )
    holdings = pt.read_holdings_file(f)
    assert [h.ticker for h in holdings] == ["AAPL", "JPM"]
    assert capsys.readouterr().out.count("Skipping invalid row") == 4


def test_read_holdings_handles_excel_bom(tmp_path):
    f = tmp_path / "h.csv"
    f.write_bytes(b"\xef\xbb\xbfticker,buy_price,shares\nAAPL,150,10\n")
    assert [h.ticker for h in pt.read_holdings_file(f)] == ["AAPL"]


def test_read_prices_file(tmp_path):
    f = tmp_path / "p.csv"
    f.write_text("ticker,price\naapl,190\nMSFT,0\nNVDA,120.5\n")
    assert pt.read_prices_file(f) == {"AAPL": 190.0, "NVDA": 120.5}


def test_yahoo_symbol_for_share_classes():
    assert pt.yahoo_symbol("BRK.B") == "BRK-B"
    assert pt.yahoo_symbol("AAPL") == "AAPL"


def test_merge_lots_uses_weighted_average_price():
    merged = pt.merge_lots([
        pt.Holding("AAPL", 100, 10),
        pt.Holding("MSFT", 300, 5),
        pt.Holding("AAPL", 120, 10),
    ])
    assert [h.ticker for h in merged] == ["AAPL", "MSFT"]
    aapl = merged[0]
    assert aapl.shares == 20
    assert aapl.buy_price == pytest.approx(110)
    assert aapl.cost_basis == pytest.approx(2200)


def test_sector_breakdown_groups_and_sorts():
    holdings = [
        priced("AAPL", 100, 1, 150, "Information Technology"),
        priced("MSFT", 100, 1, 250, "Information Technology"),
        priced("JPM", 100, 1, 100, "Financials"),
        priced("XYZ", 10, 1, 50, ""),
    ]
    rows = pt.sector_breakdown(holdings)
    assert [r[0] for r in rows] == ["Information Technology", "Financials", "Unclassified"]
    assert rows[0][1] == 400
    assert sum(r[2] for r in rows) == pytest.approx(100)
    assert rows[0][3] == 200


def test_concentration_warning_triggers_above_limit_only():
    holdings = [priced("BIG", 10, 1, 60), priced("SMALL", 10, 1, 40)]
    pt.assign_weights(holdings, pt.portfolio_totals(holdings))
    warnings = pt.concentration_warnings(holdings, max_weight=50)
    assert len(warnings) == 1 and warnings[0].startswith("BIG")
    assert pt.concentration_warnings(holdings, max_weight=60) == []


def test_price_priority_file_then_offline_manual(monkeypatch):
    companies = {"AAPL": ("Apple Inc.", "Information Technology")}
    holdings = [pt.Holding("AAPL", 150, 10), pt.Holding("ZZZ", 10, 1)]
    monkeypatch.setattr("builtins.input", lambda _: "42")
    pt.enrich_and_price(holdings, companies, offline=True, price_map={"AAPL": 190.0})
    assert (holdings[0].current_price, holdings[0].price_source) == (190.0, "file")
    assert holdings[0].company == "Apple Inc."
    assert (holdings[1].current_price, holdings[1].price_source) == (42.0, "manual")
    assert holdings[1].sector == ""


def test_live_price_used_when_available(monkeypatch):
    monkeypatch.setattr(pt, "get_live_price", lambda t: 123.45)
    h = [pt.Holding("AAPL", 100, 1)]
    pt.enrich_and_price(h, {}, offline=False)
    assert (h[0].current_price, h[0].price_source) == (123.45, "live")


def test_manual_prompt_rejects_bad_input_then_accepts(monkeypatch, capsys):
    answers = iter(["abc", "-3", "0", "nan", "$1,250.50"])
    monkeypatch.setattr("builtins.input", lambda _: next(answers))
    assert pt.ask_positive_number("> ") == 1250.50


def _sample():
    holdings = [priced("AAPL", 150, 10, 190, "Information Technology")]
    totals = pt.portfolio_totals(holdings)
    pt.assign_weights(holdings, totals)
    return holdings, totals


def test_log_created_with_header_and_total(tmp_path):
    log = tmp_path / "nested" / "log.csv"
    holdings, totals = _sample()
    pt.append_to_log(holdings, totals, log)
    rows = list(csv.DictReader(open(log, newline="")))
    assert [r["record_type"] for r in rows] == ["HOLDING", "TOTAL"]
    assert rows[0]["ticker"] == "AAPL" and rows[0]["gain_loss_pct"] == "26.67"
    assert float(rows[1]["market_value"]) == 1900


def test_log_appends_without_repeating_header(tmp_path):
    log = tmp_path / "log.csv"
    holdings, totals = _sample()
    pt.append_to_log(holdings, totals, log)
    pt.append_to_log(holdings, totals, log)
    lines = log.read_text().splitlines()
    assert lines.count(",".join(pt.LOG_FIELDS)) == 1
    assert len(lines) == 1 + 2 * 2


def test_log_with_foreign_header_is_not_corrupted(tmp_path):
    log = tmp_path / "log.csv"
    log.write_text("a,b,c\n1,2,3\n")
    holdings, totals = _sample()
    with pytest.raises(pt.LogFormatError):
        pt.append_to_log(holdings, totals, log)
    assert log.read_text() == "a,b,c\n1,2,3\n"


def test_history_change_only_for_same_portfolio(tmp_path, capsys):
    log = tmp_path / "log.csv"
    for price in (190, 200):
        h = [priced("AAPL", 150, 10, price)]
        t = pt.portfolio_totals(h)
        pt.assign_weights(h, t)
        pt.append_to_log(h, t, log)
    capsys.readouterr()
    pt.show_history(log)
    assert "Change in value since the previous run: +100.00" in capsys.readouterr().out

    h = [priced("MSFT", 300, 1, 400)]
    t = pt.portfolio_totals(h)
    pt.assign_weights(h, t)
    pt.append_to_log(h, t, log)
    capsys.readouterr()
    pt.show_history(log)
    assert "Change in value" not in capsys.readouterr().out


def test_history_without_log(tmp_path, capsys):
    pt.show_history(tmp_path / "missing.csv")
    assert "No log found" in capsys.readouterr().out


def test_end_to_end_with_sample_data(tmp_path, capsys):
    log = tmp_path / "log.csv"
    code = pt.main([
        "--file", str(DATA / "holdings_sample.csv"),
        "--prices", str(DATA / "prices_sample.csv"),
        "--log", str(log),
    ])
    out = capsys.readouterr().out
    assert code == 0
    assert "TOTAL" in out and "+13.54%" in out
    assert "Sector allocation" in out
    rows = list(csv.DictReader(open(log, newline="")))
    total = [r for r in rows if r["record_type"] == "TOTAL"][0]
    assert float(total["cost_basis"]) == 13075.75
    assert float(total["market_value"]) == 14846.0
    assert len([r for r in rows if r["record_type"] == "HOLDING"]) == 10


def test_missing_file_returns_error(tmp_path, capsys):
    assert pt.main(["--file", str(tmp_path / "nope.csv"), "--no-log"]) == 1
    assert "File not found" in capsys.readouterr().err


def test_charts_are_written(tmp_path):
    pytest.importorskip("matplotlib")
    code = pt.main([
        "--file", str(DATA / "holdings_sample.csv"),
        "--prices", str(DATA / "prices_sample.csv"),
        "--no-log", "--charts", "--chart-dir", str(tmp_path),
    ])
    assert code == 0
    for name in ("performance_by_holding.png", "sector_allocation.png"):
        assert (tmp_path / name).stat().st_size > 10_000


@pytest.fixture(scope="module")
def sp500_rows():
    with open(pt.COMPANIES_FILE, newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def test_dataset_shape_and_uniqueness(sp500_rows):
    symbols = [r["Symbol"] for r in sp500_rows]
    assert len(symbols) == len(set(symbols)) == 503
    assert all(r[c].strip() for r in sp500_rows for c in ("Symbol", "Security", "GICS Sector"))


def test_dataset_sectors_are_the_11_gics_sectors(sp500_rows):
    assert {r["GICS Sector"] for r in sp500_rows} == GICS_SECTORS


def test_dataset_loads_and_contains_sample_tickers():
    companies = pt.load_companies(pt.COMPANIES_FILE)
    assert len(companies) == 503
    with open(DATA / "holdings_sample.csv", newline="") as f:
        for row in csv.DictReader(f):
            assert row["ticker"] in companies, row["ticker"]
    assert companies["AAPL"] == ("Apple Inc.", "Information Technology")
    assert "BRK.B" in companies
