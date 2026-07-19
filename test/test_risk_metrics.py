import pytest
import math
from datetime import date
from unittest.mock import patch, MagicMock
from database_handler import DatabaseHandler
from data_parser import DataParser
from scripts.risk_calculator import RiskCalculator, generate_monthly_dates


@pytest.fixture
def risk_scenario_db(tmp_path):
    """
    Scenario for testing risk metrics:
    - 2020-01-15: Deposit 10000
    - 2020-01-16: Buy Asset A (100 shares at 100 SEK)
    """
    csv_content = """Datum;Konto;Typ av transaktion;Värdepapper/beskrivning;Antal;Kurs;Belopp;Courtage;Valuta;ISIN;Resultat
2020-01-15;1111;Insättning;Deposit;-;-;10000;0;SEK;;-
2020-01-16;1111;Köp;Asset A;100;100;-10000;0;SEK;TESTA;-
"""
    csv_file = tmp_path / "risk_scenario.csv"
    csv_file.write_text(csv_content, encoding="utf-8")

    db_file = tmp_path / "test_risk.db"
    db = DatabaseHandler(db_file)
    parser = DataParser(db)
    parser.add_data(str(csv_file))
    parser.process_transactions()
    
    # Manually inject asset prices for month ends:
    # 2020-01-31: Price 110 SEK
    # 2020-02-29: Price 121 SEK
    # 2020-03-31: Price 96.8 SEK
    db.connect()
    cur = db.get_cursor()
    cur.execute("INSERT OR REPLACE INTO asset_prices (asset_id, price_date, price, source) VALUES (1, '2020-01-31', 110.0, 'external')")
    cur.execute("INSERT OR REPLACE INTO asset_prices (asset_id, price_date, price, source) VALUES (1, '2020-02-29', 121.0, 'external')")
    cur.execute("INSERT OR REPLACE INTO asset_prices (asset_id, price_date, price, source) VALUES (1, '2020-03-31', 96.8, 'external')")
    db.commit()
    db.disconnect()
    
    return db


def test_generate_monthly_dates():
    start = date(2020, 1, 5)
    end = date(2020, 3, 15)
    dates = generate_monthly_dates(start, end)
    
    # Expected: day before start's month end (i.e. prev month end: 2019-12-31)
    # plus month ends for Jan, Feb, and end date 2020-03-15
    assert dates == [
        date(2019, 12, 31),
        date(2020, 1, 31),
        date(2020, 2, 29),
        date(2020, 3, 15)
    ]


@patch("scripts.risk_calculator.requests.get")
def test_risk_calculator_metrics(mock_get, risk_scenario_db):
    # Mock Riksbanken to return 2%
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = [{"date": "2020-03-01", "value": 2.0}]
    mock_get.return_value = mock_resp
    
    calculator = RiskCalculator(
        db=risk_scenario_db,
        accounts="1111",
        from_date="2020-01-01",
        to_date="2020-03-31",
        beta_ticker=None,
        interpolate=True
    )
    
    metrics = calculator.calculate(apy_mode='mwrr')
    
    # Portfolio values at:
    # 2019-12-31 (t_0): 0
    # 2020-01-31 (t_1): 100 shares * 110 = 11000
    # 2020-02-29 (t_2): 100 shares * 121 = 12100
    # 2020-03-31 (t_3): 100 shares * 96.8 = 9680
    
    # Cash flow in period 1: 10000 (deposit on Jan 15)
    # Return 1: (11000 - 0 - 10000) / 0 -> 0.0
    # Return 2: (12100 - 11000 - 0) / 11000 -> 0.10 (10%)
    # Return 3: (9680 - 12100 - 0) / 12100 -> -0.20 (-20%)
    # Returns series: [0.0, 0.10, -0.20]
    
    # Mean of returns: (0.0 + 0.10 - 0.20) / 3 = -0.033333 (-3.33%)
    # Variance: ((0 - (-0.033333))**2 + (0.10 - (-0.033333))**2 + (-0.20 - (-0.033333))**2) / 2
    # Variance = (0.001111 + 0.017778 + 0.027778) / 2 = 0.046667 / 2 = 0.023333
    # Monthly Stddev = sqrt(0.023333) = 0.152753
    # Annualized Stddev = 0.152753 * sqrt(12) = 0.529150 (52.92%)
    
    # Sharpe Ratio:
    # risk_free_rate = 0.02
    # overall_return = -0.1809 (-18.09%)
    # Sharpe = (-0.1809 - 0.02) / 0.529150 = -0.3798
    
    # Max Drawdown:
    # Peak = 12100
    # Trough = 9680
    # Max Drawdown = (12100 - 9680) / 12100 = 0.20 (20%)
    
    assert abs(metrics['annualized_stddev'] - 0.52915) < 0.001
    assert abs(metrics['sharpe_ratio'] - (-0.3798)) < 0.01
    assert abs(metrics['max_drawdown'] - 0.20) < 0.001
    assert metrics['max_drawdown_peak'] == date(2020, 2, 29)
    assert metrics['max_drawdown_trough'] == date(2020, 3, 31)
    
    # Verify Riksbanken was called with correct parameters
    mock_get.assert_called_once()
    args, kwargs = mock_get.call_args
    assert "SECBREPOEFF" in args[0]
    assert "2020-01-01" in args[0]
    assert "2020-03-31" in args[0]


@patch("scripts.risk_calculator.requests.get")
def test_riksbanken_rate_fallback(mock_get, risk_scenario_db):
    # Mock Riksbanken API failure (connection error or HTTP error)
    mock_get.side_effect = Exception("Connection error")
    
    calculator = RiskCalculator(
        db=risk_scenario_db,
        accounts="1111",
        from_date="2020-01-01",
        to_date="2020-03-31",
        beta_ticker=None,
        interpolate=True
    )
    
    metrics = calculator.calculate(apy_mode='mwrr')
    
    # Should fall back to 2.0%
    assert metrics['risk_free_rate'] == 0.02
    assert abs(metrics['sharpe_ratio'] - (-0.3798)) < 0.01


@patch("scripts.risk_calculator.requests.get")
def test_beta_calculation(mock_get, risk_scenario_db):
    # We call calculate twice: once for Riksbanken, once for Yahoo Finance chart
    # Mock Riksbanken response
    mock_riksbanken_resp = MagicMock()
    mock_riksbanken_resp.status_code = 200
    mock_riksbanken_resp.json.return_value = [{"date": "2020-03-01", "value": 0.0}]
    
    # Mock Yahoo Finance response
    # Timestamps correspond to 2019-12-31, 2020-01-31, 2020-02-29, 2020-03-31
    import time
    ts_0 = int(time.mktime(date(2019, 12, 31).timetuple()))
    ts_1 = int(time.mktime(date(2020, 1, 31).timetuple()))
    ts_2 = int(time.mktime(date(2020, 2, 29).timetuple()))
    ts_3 = int(time.mktime(date(2020, 3, 31).timetuple()))
    
    mock_yahoo_resp = MagicMock()
    mock_yahoo_resp.status_code = 200
    mock_yahoo_resp.json.return_value = {
        "chart": {
            "result": [
                {
                    "timestamp": [ts_0, ts_1, ts_2, ts_3],
                    "indicators": {
                        "quote": [{"close": [100.0, 105.0, 110.25, 99.225]}]  # returns: [0.05, 0.05, -0.10]
                    }
                }
            ]
        }
    }
    
    # Side effects for Riksbanken first, then Yahoo Finance
    mock_get.side_effect = [mock_riksbanken_resp, mock_yahoo_resp]
    
    calculator = RiskCalculator(
        db=risk_scenario_db,
        accounts="1111",
        from_date="2020-01-01",
        to_date="2020-03-31",
        beta_ticker="^OMXSPI",
        interpolate=True
    )
    
    metrics = calculator.calculate(apy_mode='mwrr')
    
    # Port returns: [0.0, 0.10, -0.20], mean: -0.033333
    # Bench returns: [0.05, 0.05, -0.10], mean: 0.0
    
    # Covariance:
    # ((0 - (-0.033333)) * (0.05 - 0) + (0.10 - (-0.033333)) * (0.05 - 0) + (-0.20 - (-0.033333)) * (-0.10 - 0)) / 2
    # = (0.033333 * 0.05 + 0.133333 * 0.05 + (-0.166667) * (-0.10)) / 2
    # = (0.001667 + 0.006667 + 0.016667) / 2 = 0.025 / 2 = 0.0125
    
    # Var(bench):
    # ((0.05 - 0)**2 + (0.05 - 0)**2 + (-0.10 - 0)**2) / 2
    # = (0.0025 + 0.0025 + 0.01) / 2 = 0.015 / 2 = 0.0075
    
    # Beta = Covariance / Var(bench) = 0.0125 / 0.0075 = 1.6667
    
    assert metrics['beta'] is not None
    assert abs(metrics['beta'] - 1.6667) < 0.01
