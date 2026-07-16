import pytest
import argparse
import json
import sys
from datetime import date
from database_handler import DatabaseHandler
from data_parser import DataParser
from calculate_stats import StatCalculator
from cli import stats, portfolio

@pytest.fixture
def stats_positions_test_db(tmp_path):
    # Set up test database with synthetic transactions
    csv_content = """Datum;Konto;Typ av transaktion;Värdepapper/beskrivning;Antal;Kurs;Belopp;Courtage;Valuta;ISIN;Resultat
2025-01-01;1111;Insättning;Deposit;-;-;10000;0;SEK;;-
2025-01-02;1111;Köp;Mock Fund A;10;100;-1000;0;SEK;MOCKA;-
2025-06-01;1111;Insättning;Deposit;-;-;5000;0;SEK;;-
2025-06-02;1111;Köp;Mock Fund B;5;200;-1000;0;SEK;MOCKB;-
"""
    csv_file = tmp_path / "stats_positions.csv"
    csv_file.write_text(csv_content, encoding="utf-8")
    
    db_file = tmp_path / "test_stats_positions.db"
    db = DatabaseHandler(db_file)
    
    parser = DataParser(db)
    parser.add_data(str(csv_file))
    parser.process_transactions()
    
    # Pre-populate asset prices for both assets at different times
    cur = db.get_cursor()
    # Prices on 2025-01-02
    cur.execute("INSERT OR REPLACE INTO asset_prices (asset_id, price_date, price, source) VALUES (1, '2025-01-02', 100.0, 'external')")
    # Prices on 2025-06-02
    cur.execute("INSERT OR REPLACE INTO asset_prices (asset_id, price_date, price, source) VALUES (2, '2025-06-02', 200.0, 'external')")
    # Prices as of 2025-12-31 (Fund A grew to 150, Fund B grew to 250)
    cur.execute("INSERT OR REPLACE INTO asset_prices (asset_id, price_date, price, source) VALUES (1, '2025-12-31', 150.0, 'external')")
    cur.execute("INSERT OR REPLACE INTO asset_prices (asset_id, price_date, price, source) VALUES (2, '2025-12-31', 250.0, 'external')")
    db.commit()
    
    stat_calc = StatCalculator(db)
    stat_calc.calculate_stats(apy_mode='mwrr')
    
    return db_file

def test_stats_positions(stats_positions_test_db, capsys):
    args = argparse.Namespace(
        database=str(stats_positions_test_db),
        account='1111',
        period='default',
        deposits='current',
        accumulated=False,
        update_prices='never',
        update_all=False,
        force=False,
        apy_mode='mwrr',
        as_of='2025-12-31',
        cohorts_start=None,
        cohorts_end=None,
        value_start=None,
        value_end=None,
        start=None,
        end=None,
        positions=True,
        summary=False,
        format='table',
        quiet=True
    )
    
    stats(args)
    captured = capsys.readouterr()
    
    # Verify both cohorts (Dec 2024 and May 2025) are printed with their holdings
    assert "Dec 2024" in captured.out
    assert "Mock Fund A" in captured.out
    assert "May 2025" in captured.out
    assert "Mock Fund B" in captured.out

def test_stats_summary_only(stats_positions_test_db, capsys):
    args = argparse.Namespace(
        database=str(stats_positions_test_db),
        account='1111',
        period='default',
        deposits='current',
        accumulated=False,
        update_prices='never',
        update_all=False,
        force=False,
        apy_mode='mwrr',
        as_of='2025-12-31',
        cohorts_start=None,
        cohorts_end=None,
        value_start=None,
        value_end=None,
        start=None,
        end=None,
        positions=False,
        summary=True,
        format='table',
        quiet=True
    )
    
    stats(args)
    captured = capsys.readouterr()
    
    # Cohort breakdown should be suppressed in summary mode
    assert "Dec 2024" not in captured.out
    assert "Summary — all cohorts" in captured.out
    assert "Total Deposited:" in captured.out
    assert "Current Value:" in captured.out

def test_stats_summary_positions_json(stats_positions_test_db, capsys):
    args = argparse.Namespace(
        database=str(stats_positions_test_db),
        account='1111',
        period='default',
        deposits='current',
        accumulated=False,
        update_prices='never',
        update_all=False,
        force=False,
        apy_mode='mwrr',
        as_of='2025-12-31',
        cohorts_start=None,
        cohorts_end=None,
        value_start=None,
        value_end=None,
        start=None,
        end=None,
        positions=True,
        summary=True,
        format='json',
        quiet=True
    )
    
    stats(args)
    captured = capsys.readouterr()
    
    data = json.loads(captured.out)
    assert 'deposits' in data
    assert 'current_value' in data
    assert 'holdings' in data
    assert len(data['holdings']) == 2
    assert data['holdings'][0]['asset'] == "Mock Fund A"
    assert data['holdings'][1]['asset'] == "Mock Fund B"

def test_portfolio_alias(stats_positions_test_db, capsys):
    args = argparse.Namespace(
        database=str(stats_positions_test_db),
        account='1111',
        as_of='2025-12-31',
        cohorts_start=None,
        cohorts_end=None,
        value_start=None,
        value_end=None,
        start=None,
        end=None,
        apy_mode='mwrr',
        format='json',
        quiet=True
    )
    
    portfolio(args)
    captured = capsys.readouterr()
    
    data = json.loads(captured.out)
    assert 'holdings' in data
    assert len(data['holdings']) == 2

def test_cohorts_filtering(stats_positions_test_db, capsys):
    # Only show cohorts starting from 2025-05-01
    args = argparse.Namespace(
        database=str(stats_positions_test_db),
        account='1111',
        period='default',
        deposits='current',
        accumulated=False,
        update_prices='never',
        update_all=False,
        force=False,
        apy_mode='mwrr',
        as_of='2025-12-31',
        cohorts_start='2025-05-01',
        cohorts_end=None,
        value_start=None,
        value_end=None,
        start=None,
        end=None,
        positions=False,
        summary=False,
        format='table',
        quiet=True
    )
    
    stats(args)
    captured = capsys.readouterr()
    
    assert "Dec 2024" not in captured.out
    assert "May 2025" in captured.out

def test_period_valuation(stats_positions_test_db, capsys):
    # Calculate performance specifically between 2025-06-01 and 2025-12-31
    args = argparse.Namespace(
        database=str(stats_positions_test_db),
        account='1111',
        period='default',
        deposits='current',
        accumulated=False,
        update_prices='never',
        update_all=False,
        force=False,
        apy_mode='mwrr',
        as_of=None,
        cohorts_start=None,
        cohorts_end=None,
        value_start='2025-06-01',
        value_end='2025-12-31',
        start=None,
        end=None,
        positions=False,
        summary=False,
        format='json',
        quiet=True
    )
    
    stats(args)
    captured = capsys.readouterr()
    
    data = json.loads(captured.out)
    # Check that performance is filtered
    assert len(data) > 0
