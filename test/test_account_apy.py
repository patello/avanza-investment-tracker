import sys
sys.path.insert(0, "..")
import pytest
from datetime import date
from database_handler import DatabaseHandler
from data_parser import DataParser
from calculate_stats import StatCalculator

@pytest.fixture
def apy_test_db(tmp_path):
    # Set up a database with:
    # 2020-01-01: Deposit 10000 SEK (Account 1111)
    # 2020-01-02: Buy Asset A (100 shares @ 100 SEK = 10000 SEK)
    # 2020-07-01: Deposit 5000 SEK (Account 1111)
    # 2020-07-02: Buy Asset A (50 shares @ 100 SEK = 5000 SEK)
    csv_content = """Datum;Konto;Typ av transaktion;Värdepapper/beskrivning;Antal;Kurs;Belopp;Courtage;Valuta;ISIN;Resultat
2020-01-01;1111;Insättning;Deposit;-;-;10000;0;SEK;;-
2020-01-02;1111;Köp;Asset A;100;100;-10000;0;SEK;TESTA;-
2020-07-01;1111;Insättning;Deposit;-;-;5000;0;SEK;;-
2020-07-02;1111;Köp;Asset A;50;100;-5000;0;SEK;TESTA;-
"""
    csv_file = tmp_path / "apy_test.csv"
    csv_file.write_text(csv_content, encoding="utf-8")
    
    db_file = tmp_path / "test_account_apy.db"
    db = DatabaseHandler(db_file)
    
    parser = DataParser(db)
    parser.add_data(str(csv_file))
    parser.process_transactions()
    
    return db

def test_account_apy_mwrr(apy_test_db):
    # Set asset price for Asset A to 120 SEK as of 2020-12-31
    cur = apy_test_db.get_cursor()
    cur.execute("UPDATE assets SET latest_price = 120.0, latest_price_date = '2020-12-31'")
    cur.execute("INSERT OR REPLACE INTO asset_prices (asset_id, price_date, price, source) VALUES (1, '2020-12-31', 120.0, 'external')")
    apy_test_db.commit()
    
    stat_calc = StatCalculator(apy_test_db)
    # Run stats calculation
    stat_calc.calculate_stats(apy_mode='mwrr')
    
    # 1. MWRR calculation over the year
    # End date: 2020-12-31 (365 days from 2020-01-01)
    # Deposits: 10000 (day 0), 5000 (day 182)
    # Valuation: 150 shares * 120 SEK = 18000 SEK
    # Net invested: 15000
    # Gain: 3000
    # Weight for CF2: (365 - 182)/365 = 183/365 ≈ 0.50137
    # Denominator = 10000 * 1.0 + 5000 * 0.50137 = 12506.85
    # HPR = 3000 / 12506.85 ≈ 0.23987
    # APY ≈ 23.99%
    apy, total_days = stat_calc.calculate_account_apy('1111', apy_mode='mwrr', end_date=date(2020, 12, 31))
    
    assert total_days == 365
    assert apy is not None
    assert 23.9 <= apy <= 24.1
    
def test_account_apy_twrr(apy_test_db):
    cur = apy_test_db.get_cursor()
    cur.execute("UPDATE assets SET latest_price = 120.0, latest_price_date = '2020-12-31'")
    cur.execute("INSERT OR REPLACE INTO asset_prices (asset_id, price_date, price, source) VALUES (1, '2020-12-31', 120.0, 'external')")
    apy_test_db.commit()
    
    stat_calc = StatCalculator(apy_test_db)
    stat_calc.calculate_stats(apy_mode='twrr')
    
    apy, total_days = stat_calc.calculate_account_apy('1111', apy_mode='twrr', end_date=date(2020, 12, 31))
    assert apy is not None
    assert 19.8 <= apy <= 20.2
