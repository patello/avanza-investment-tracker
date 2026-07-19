import shutil
import os
import time
import math
import logging
import requests
from datetime import date, datetime as datetime_cls
from data_parser import DataParser, SpecialCases
from database_handler import DatabaseHandler

class HistoricalTracker(DataParser):
    """
    Subclass of DataParser that tracks and records historical cash balances
    and asset holdings chronologically during the transaction processing loop.
    """
    def __init__(self, db: DatabaseHandler, cohorts_start=None, cohorts_end=None, special_cases=None):
        super().__init__(db, special_cases)
        self.cohorts_start = cohorts_start
        self.cohorts_end = cohorts_end
        # Map date -> { 'cash': {account: amount}, 'assets': {account: {asset_id: amount}} }
        self.history = {}

    def get_current_state(self):
        """Query database tables to construct the current snapshot of cash and assets."""
        query_cash = "SELECT account, SUM(capital) FROM cohort_data"
        params_cash = []
        if self.cohorts_start or self.cohorts_end:
            query_cash += " WHERE 1=1"
            if self.cohorts_start:
                query_cash += " AND month >= ?"
                params_cash.append(self.cohorts_start.isoformat())
            if self.cohorts_end:
                query_cash += " AND month <= ?"
                params_cash.append(self.cohorts_end.isoformat())
        query_cash += " GROUP BY account"
        self.data_cur.execute(query_cash, params_cash)
        cash = {row[0]: row[1] for row in self.data_cur.fetchall()}

        query_assets = "SELECT account, asset_id, SUM(amount) FROM cohort_assets WHERE amount > 0"
        params_assets = []
        if self.cohorts_start or self.cohorts_end:
            if self.cohorts_start:
                query_assets += " AND month >= ?"
                params_assets.append(self.cohorts_start.isoformat())
            if self.cohorts_end:
                query_assets += " AND month <= ?"
                params_assets.append(self.cohorts_end.isoformat())
        query_assets += " GROUP BY account, asset_id"
        self.data_cur.execute(query_assets, params_assets)
        assets = {}
        for account, asset_id, amount in self.data_cur.fetchall():
            if account not in assets:
                assets[account] = {}
            assets[account][asset_id] = amount
            
        return {
            'cash': cash,
            'assets': assets
        }

    def process_transactions(self, raise_on_unprocessed: bool = True) -> None:
        """
        Modified transaction processing loop that records portfolio holdings
        state at the end of each date.
        """
        unprocessed_lines = self.transaction_cur.execute(
            "SELECT *, rowid FROM transactions WHERE processed == 0 ORDER BY date ASC, rowid ASC"
        )
        row = unprocessed_lines.fetchone()
        
        max_date_processed = None
        while row is not None:
            # Re-apply special cases if defined
            if self.special_cases is not None:
                row_data = row[:10]
                updated_row_data = self.special_cases.handle_special_cases(row_data)
                final_row_data = list(updated_row_data)
                for idx in (4, 5, 6, 7):
                    if isinstance(final_row_data[idx], str) and final_row_data[idx] not in ('-', ''):
                        try:
                            final_row_data[idx] = float(final_row_data[idx].replace(',', '.'))
                        except ValueError:
                            pass
                updated_row_data = tuple(final_row_data)
                if updated_row_data != row_data:
                    rowid = row[11]
                    self.data_cur.execute("""
                        UPDATE transactions
                        SET date = ?, account = ?, transaction_type = ?, asset_name = ?,
                            amount = ?, price = ?, total = ?, courtage = ?, currency = ?, isin = ?
                        WHERE rowid = ?
                    """, updated_row_data + (rowid,))
                    row = updated_row_data + (0, rowid)

            t_date_str = row[0]
            if isinstance(t_date_str, str):
                t_date = datetime_cls.strptime(t_date_str, "%Y-%m-%d").date()
            else:
                t_date = t_date_str

            tx_type = row[2]
            if tx_type in ("Insättning", "Autogiroinsättning"):
                self.handle_deposit(row)
            elif tx_type == "Uttag":
                self.handle_withdrawal(row)
            elif tx_type == "Köp":
                self.handle_purchase(row)
            elif tx_type == "Sälj":
                self.handle_sale(row)
            elif tx_type == "Utdelning":
                self.handle_dividend(row)
            elif tx_type in ("Räntor", "Ränta", "Inlåningsränta", "Utlåningsränta", "Uttag av riskkostnad"):
                if row[6] > 0:
                    self.handle_interest(row)
                else:
                    self.handle_fees(row)
            elif tx_type == "Utbokning fraktioner":
                self.handle_ignore(row)
            elif any(tax in tx_type for tax in ("Utländsk källskatt", "Prelskatt", "Preliminärskatt", "Avkastningsskatt")):
                self.handle_fees(row)
            elif "Byte" in tx_type or tx_type == "Övrigt":
                self.handle_listing_change(row)
            elif tx_type == "Tillgångsinsättning":
                self.handle_asset_deposit(row)
            elif tx_type == "Intern överföring":
                self.handle_internal_transfer(row)
            elif tx_type == "Värdepappersinsättning":
                self.handle_ignore(row)
            elif tx_type == "Värdepappersuttag":
                if row[4] < 0:
                    self.handle_remove_shares(row)
                else:
                    self.handle_ignore(row)
            else:
                raise ValueError(f"Unknown transaction type: {tx_type}")

            # Record state after processing this transaction
            if max_date_processed is None or t_date >= max_date_processed:
                max_date_processed = t_date
                self.history[t_date] = self.get_current_state()
            row = unprocessed_lines.fetchone()

        unprocessed_count = self.transaction_cur.execute(
            "SELECT COUNT(*) FROM transactions WHERE processed == 0"
        ).fetchone()[0]
        
        if unprocessed_count > 0:
            if raise_on_unprocessed:
                from data_parser import AssetDeficit
                raise AssetDeficit(
                    f"There are {unprocessed_count} transaction(s) that could not be processed "
                    "due to a mismatch of assets in the database", self
                )
            else:
                logging.warning(f"There are {unprocessed_count} transaction(s) left unprocessed (normal for temporary historical snapshots)")


def generate_monthly_dates(start_date: date, end_date: date) -> list:
    """Generate monthly end-of-month dates between start_date and end_date."""
    import calendar
    
    dates = []
    # Start with the end of the month prior to start_date
    if start_date.month == 1:
        prev_month_end = date(start_date.year - 1, 12, 31)
    else:
        prev_month_end = date(
            start_date.year,
            start_date.month - 1,
            calendar.monthrange(start_date.year, start_date.month - 1)[1]
        )
    dates.append(prev_month_end)
    
    curr = start_date
    while True:
        last_day = calendar.monthrange(curr.year, curr.month)[1]
        month_end = date(curr.year, curr.month, last_day)
        
        if month_end >= end_date:
            break
            
        dates.append(month_end)
        
        if curr.month == 12:
            curr = date(curr.year + 1, 1, 1)
        else:
            curr = date(curr.year, curr.month + 1, 1)
            
    dates.append(end_date)
    
    # Remove duplicates
    unique_dates = []
    for d in dates:
        if d not in unique_dates:
            unique_dates.append(d)
            
    return unique_dates


def get_holdings_at_date(history: dict, target_date: date, accounts_filter: list) -> tuple:
    """Find the holdings (cash and asset shares) as of a target date."""
    import bisect
    tx_dates = sorted(history.keys())
    idx = bisect.bisect_right(tx_dates, target_date)
    
    if idx == 0:
        return 0.0, {}
        
    d = tx_dates[idx - 1]
    state = history[d]
    
    cash = 0.0
    for acc, val in state['cash'].items():
        if accounts_filter is None or acc in accounts_filter:
            cash += val
            
    assets_shares = {}
    for acc, assets in state['assets'].items():
        if accounts_filter is None or acc in accounts_filter:
            for asset_id, amount in assets.items():
                assets_shares[asset_id] = assets_shares.get(asset_id, 0.0) + amount
                
    return cash, assets_shares


def fetch_riksbanken_rate(from_date: date, to_date: date) -> float:
    """
    Fetch the average repo rate (policy rate) from Riksbanken SWEA API.
    Fallback to 2.0% (0.02) if offline or call fails.
    """
    url = f"https://api.riksbank.se/swea/v1/Observations/SECBREPOEFF/{from_date.isoformat()}/{to_date.isoformat()}"
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json"
    }
    try:
        r = requests.get(url, headers=headers, timeout=5)
        if r.status_code == 200:
            data = r.json()
            if isinstance(data, list) and len(data) > 0:
                rates = [obs["value"] for obs in data if "value" in obs]
                if rates:
                    return (sum(rates) / len(rates)) / 100.0
    except Exception as e:
        logging.debug(f"Failed to fetch Riksbanken policy rate: {e}")
    return 0.02


def fetch_yahoo_benchmark_prices(ticker: str, start_date: date, end_date: date) -> list:
    """Fetch daily closing prices for a benchmark ticker from Yahoo Finance."""
    import urllib.parse
    import time
    
    encoded_ticker = urllib.parse.quote(ticker)
    start_ts = int(time.mktime(start_date.timetuple()))
    end_ts = int(time.mktime(end_date.timetuple()))
    
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{encoded_ticker}?period1={start_ts}&period2={end_ts}&interval=1d"
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"
        )
    }
    try:
        r = requests.get(url, headers=headers, timeout=10)
        if r.status_code == 200:
            data = r.json()
            result = data.get("chart", {}).get("result", [])
            if result:
                res = result[0]
                timestamps = res.get("timestamp", [])
                indicators = res.get("indicators", {})
                
                adjclose_list = indicators.get("adjclose", [{}])[0].get("adjclose", [])
                close_list = indicators.get("quote", [{}])[0].get("close", [])
                prices = adjclose_list if adjclose_list else close_list
                
                bench_prices = []
                for ts, price in zip(timestamps, prices):
                    if price is not None:
                        dt = date.fromtimestamp(ts)
                        bench_prices.append((dt, float(price)))
                return sorted(bench_prices, key=lambda x: x[0])
        else:
            logging.warning(f"Failed to fetch Yahoo Finance benchmark prices for {ticker} (Status: {r.status_code})")
    except Exception as e:
        logging.warning(f"Failed to fetch Yahoo Finance benchmark prices for {ticker}: {e}")
    return []


def get_benchmark_price(bench_prices: list, target_date: date) -> float:
    """Get the benchmark price closest to or on the target date."""
    if not bench_prices:
        return 0.0
    import bisect
    dates = [x[0] for x in bench_prices]
    idx = bisect.bisect_right(dates, target_date)
    if idx == 0:
        return bench_prices[0][1]
    return bench_prices[idx - 1][1]


class RiskCalculator:
    """Reconstructs portfolio history and calculates risk metrics."""
    def __init__(self, db: DatabaseHandler, accounts=None, from_date=None, to_date=None, 
                 cohorts_start=None, cohorts_end=None, beta_ticker=None, interpolate=True):
        self.db = db
        self.accounts = accounts if accounts is not None else 'all'
        self.from_date = from_date
        self.to_date = to_date
        self.cohorts_start = cohorts_start
        self.cohorts_end = cohorts_end
        self.beta_ticker = beta_ticker
        self.interpolate = interpolate

    def calculate(self, apy_mode='mwrr') -> dict:
        """Perform the chronological reconstruction and calculate all metrics."""
        from cli import resolve_accounts
        cur = self.db.get_cursor()
        
        accounts_list = resolve_accounts(self.db, self.accounts)
        
        # 1. Resolve date bounds
        to_date = self.to_date
        if to_date is None:
            to_date = date.today()
        elif isinstance(to_date, str):
            to_date = datetime_cls.strptime(to_date, "%Y-%m-%d").date()
            
        from_date = self.from_date
        if isinstance(from_date, str):
            from_date = datetime_cls.strptime(from_date, "%Y-%m-%d").date()
            
        if from_date is None:
            if self.cohorts_start is not None:
                from_date = self.cohorts_start
            else:
                if accounts_list:
                    placeholders = ",".join("?" for _ in accounts_list)
                    query = f"SELECT MIN(date) FROM transactions WHERE account IN ({placeholders})"
                    cur.execute(query, accounts_list)
                else:
                    cur.execute("SELECT MIN(date) FROM transactions")
                row = cur.fetchone()
                first_tx_date = row[0] if row else None
                if first_tx_date:
                    if isinstance(first_tx_date, str):
                        from_date = datetime_cls.strptime(first_tx_date, "%Y-%m-%d").date()
                    else:
                        from_date = first_tx_date
                else:
                    from_date = date(2018, 1, 1)

        # Ensure correct date order
        if from_date > to_date:
            raise ValueError(f"Start date {from_date} cannot be after end date {to_date}")

        # 2. Reconstruct historical holdings state
        history, deposits_by_month, cf_by_month = self.get_historical_holdings(to_date)
        sampling_dates = generate_monthly_dates(from_date, to_date)
        
        # 3. Calculate portfolio values at each monthly end date
        portfolio_values = []
        for t in sampling_dates:
            cash, assets_shares = get_holdings_at_date(history, t, accounts_list)
            asset_value = 0.0
            for asset_id, shares in assets_shares.items():
                if shares > 0.0001:
                    price, _, _, _ = self.db.get_price(asset_id, t, interpolate=self.interpolate)
                    if price is not None and price > 0:
                        asset_value += shares * price
            portfolio_values.append(cash + asset_value)

        # 3b. Trim leading zero-value months (common when evaluating specific cohorts or recently opened accounts)
        first_val_idx = None
        for idx, val in enumerate(portfolio_values):
            if val > 0.01:
                first_val_idx = idx
                break
        if first_val_idx is not None and first_val_idx > 0:
            start_slice = first_val_idx - 1
            sampling_dates = sampling_dates[start_slice:]
            portfolio_values = portfolio_values[start_slice:]

        # 4. Fetch daily cash flows (from cohort-specific tables)
        from collections import defaultdict
        cash_flows_by_date = defaultdict(float)
        
        for m_str, dep in deposits_by_month.items():
            m_date = datetime_cls.strptime(m_str, "%Y-%m-%d").date() if isinstance(m_str, str) else m_str
            cash_flows_by_date[m_date] += dep
            
        for m_str, cf in cf_by_month.items():
            m_date = datetime_cls.strptime(m_str, "%Y-%m-%d").date() if isinstance(m_str, str) else m_str
            cash_flows_by_date[m_date] += cf

        cash_flows = []
        for i in range(1, len(sampling_dates)):
            t_start = sampling_dates[i-1]
            t_end = sampling_dates[i]
            period_cf = 0.0
            for d, cf in cash_flows_by_date.items():
                if i == 1:
                    if t_start <= d <= t_end:
                        period_cf += cf
                else:
                    if t_start < d <= t_end:
                        period_cf += cf
            cash_flows.append(period_cf)

        # 5. Compute monthly returns
        returns = []
        for i in range(1, len(sampling_dates)):
            v_start = portfolio_values[i-1]
            v_end = portfolio_values[i]
            cf = cash_flows[i-1]
            
            if v_start > 0.01:
                r = (v_end - v_start - cf) / v_start
            else:
                r = 0.0
            returns.append(r)

        # 6. Fetch Risk-Free Rate
        rf_rate = fetch_riksbanken_rate(from_date, to_date)
        
        # 7. Calculate overall return (APY) of the specific period
        total_days = (to_date - from_date).days
        if total_days <= 0:
            overall_return = 0.0
        elif apy_mode == 'twrr':
            # TWRR is the compounded product of sub-period returns
            prod = 1.0
            for r in returns:
                prod *= (1.0 + r)
            years = total_days / 365.25
            overall_return = (prod ** (1.0 / years) - 1.0) if prod >= 0 else 0.0
        else:
            # MWRR (Modified Dietz) for the specific period
            sum_w_cf = 0.0
            for cf_date, cf_amount in cash_flows_by_date.items():
                if from_date <= cf_date <= to_date:
                    days_elapsed = (cf_date - from_date).days
                    weight = (total_days - days_elapsed) / total_days
                    sum_w_cf += weight * cf_amount
                    
            hpr_denominator = portfolio_values[0] + sum_w_cf
            if hpr_denominator > 0.01:
                hpr = (portfolio_values[-1] - portfolio_values[0] - sum(cash_flows)) / hpr_denominator
                years = total_days / 365.25
                overall_return = ((1.0 + hpr) ** (1.0 / years) - 1.0) if (1.0 + hpr) >= 0 else -1.0 + (1.0 + hpr)
            else:
                overall_return = 0.0

        # 8. Calculate risk metrics
        n = len(returns)
        if n < 2:
            annualized_stddev = 0.0
            sharpe = 0.0
            sortino = 0.0
        else:
            mean_r = sum(returns) / n
            variance = sum((r - mean_r) ** 2 for r in returns) / (n - 1)
            stddev = math.sqrt(variance)
            annualized_stddev = stddev * math.sqrt(12)
            
            if annualized_stddev > 0.0001:
                sharpe = (overall_return - rf_rate) / annualized_stddev
            else:
                sharpe = 0.0
                
            rf_monthly = rf_rate / 12.0
            downside_diffs = [min(r - rf_monthly, 0.0) for r in returns]
            downside_variance = sum(diff ** 2 for diff in downside_diffs) / (n - 1)
            downside_stddev = math.sqrt(downside_variance)
            annualized_downside_stddev = downside_stddev * math.sqrt(12)
            
            if annualized_downside_stddev > 0.0001:
                sortino = (overall_return - rf_rate) / annualized_downside_stddev
            else:
                sortino = 0.0

        # 9. Maximum Drawdown (calculated on cumulative return index to isolate from cash flows)
        max_dd = 0.0
        peak = 1.0
        peak_date = sampling_dates[0]
        trough_date = None
        current_peak_date = sampling_dates[0]
        
        index_value = 1.0
        index_values = [1.0]
        for r in returns:
            index_value *= (1.0 + r)
            index_values.append(index_value)
            
        for val, dt in zip(index_values, sampling_dates):
            if val > peak:
                peak = val
                current_peak_date = dt
            if peak > 0.0001:
                dd = (peak - val) / peak
                if dd > max_dd:
                    max_dd = dd
                    peak_date = current_peak_date
                    trough_date = dt

        # 10. Beta vs benchmark
        beta = None
        if self.beta_ticker:
            bench_prices = fetch_yahoo_benchmark_prices(self.beta_ticker, sampling_dates[0], to_date)
            if bench_prices:
                bench_values = [get_benchmark_price(bench_prices, t) for t in sampling_dates]
                bench_returns = []
                for i in range(1, len(sampling_dates)):
                    b_start = bench_values[i-1]
                    b_end = bench_values[i]
                    if b_start > 0.0001:
                        r_bench = (b_end - b_start) / b_start
                    else:
                        r_bench = 0.0
                    bench_returns.append(r_bench)
                    
                if len(bench_returns) >= 2:
                    mean_bench = sum(bench_returns) / len(bench_returns)
                    mean_port = sum(returns) / len(returns)
                    
                    covariance = sum((r_p - mean_port) * (r_b - mean_bench) for r_p, r_b in zip(returns, bench_returns)) / (len(returns) - 1)
                    variance_bench = sum((r_b - mean_bench) ** 2 for r_b in bench_returns) / (len(bench_returns) - 1)
                    
                    if variance_bench > 1e-8:
                        beta = covariance / variance_bench

        # 11. Returns by Calendar Year
        year_returns = {}
        unique_years = sorted(list(set(d.year for d in sampling_dates[1:])))
        for y in unique_years:
            y_indices = [i for i, d in enumerate(sampling_dates) if d.year == y]
            if y_indices:
                first_idx = y_indices[0]
                last_idx = y_indices[-1]
                
                if first_idx == 0:
                    t_start_idx = 0
                    t_end_idx = last_idx
                    y_cf = sum(cash_flows[j] for j in range(0, last_idx))
                else:
                    t_start_idx = first_idx - 1
                    t_end_idx = last_idx
                    y_cf = sum(cash_flows[j] for j in range(t_start_idx, t_end_idx))
                
                v_start = portfolio_values[t_start_idx]
                v_end = portfolio_values[t_end_idx]
                
                if v_start > 0.01:
                    r_year = (v_end - v_start - y_cf) / v_start
                elif y_cf > 0.01:
                    r_year = (v_end - v_start - y_cf) / y_cf
                else:
                    r_year = 0.0
                year_returns[y] = r_year

        return {
            'period_start': sampling_dates[0],
            'period_end': to_date,
            'annualized_return': overall_return,
            'annualized_stddev': annualized_stddev,
            'sharpe_ratio': sharpe,
            'sortino_ratio': sortino,
            'max_drawdown': max_dd,
            'max_drawdown_peak': peak_date,
            'max_drawdown_trough': trough_date,
            'beta': beta,
            'risk_free_rate': rf_rate,
            'year_returns': year_returns
        }

    def get_historical_holdings(self, end_date: date) -> dict:
        """Run transaction parser chronologically in a temporary DB and capture holdings."""
        db_file = self.db.db_file
        temp_db_path = f"{db_file}_temp_risk_{int(time.time())}_{id(self)}.db"
        shutil.copy2(db_file, temp_db_path)
        
        temp_db = DatabaseHandler(temp_db_path)
        temp_db.interpolate = self.interpolate
        temp_db.connect()
        
        temp_cur = temp_db.get_cursor()
        
        # We delete transactions after end_date to avoid parsing future events
        temp_cur.execute("DELETE FROM transactions WHERE date > ?", (end_date.isoformat(),))
        
        temp_db.reset_table("cohort_data")
        temp_db.reset_table("cohort_assets")
        temp_db.reset_table("cohort_cash_flows")
        temp_db.reset_table("assets")
        temp_cur.execute("UPDATE transactions SET processed = 0")
        temp_db.commit()
        
        special_cases_path = "data/special_cases.json"
        special_cases = SpecialCases(special_cases_path) if os.path.exists(special_cases_path) else None
        
        tracker = HistoricalTracker(temp_db, self.cohorts_start, self.cohorts_end, special_cases)
        try:
            tracker.process_transactions(raise_on_unprocessed=False)
            history = tracker.history
            
            # Query cohort-specific deposits and cash flows
            cur = temp_db.get_cursor()
            
            query_dep = "SELECT month, SUM(deposit) FROM cohort_data"
            params_dep = []
            if self.cohorts_start or self.cohorts_end:
                query_dep += " WHERE 1=1"
                if self.cohorts_start:
                    query_dep += " AND month >= ?"
                    params_dep.append(self.cohorts_start.isoformat())
                if self.cohorts_end:
                    query_dep += " AND month <= ?"
                    params_dep.append(self.cohorts_end.isoformat())
            query_dep += " GROUP BY month"
            cur.execute(query_dep, params_dep)
            deposits_by_month = {row[0]: row[1] for row in cur.fetchall()}
            
            query_cf = "SELECT transaction_month, SUM(amount) FROM cohort_cash_flows"
            params_cf = []
            if self.cohorts_start or self.cohorts_end:
                query_cf += " WHERE 1=1"
                if self.cohorts_start:
                    query_cf += " AND cohort_month >= ?"
                    params_cf.append(self.cohorts_start.isoformat())
                if self.cohorts_end:
                    query_cf += " AND cohort_month <= ?"
                    params_cf.append(self.cohorts_end.isoformat())
            query_cf += " GROUP BY transaction_month"
            cur.execute(query_cf, params_cf)
            cf_by_month = {row[0]: row[1] for row in cur.fetchall()}
            
        finally:
            temp_db.disconnect()
            try:
                os.remove(temp_db_path)
            except Exception:
                pass
                
        return history, deposits_by_month, cf_by_month
