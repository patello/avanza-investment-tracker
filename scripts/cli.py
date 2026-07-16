#!/usr/bin/env python3
"""
CLI interface for the Avanza investment tracker.

Provides command-line access to data import, transaction processing,
price updates, and statistics calculation.
"""

import argparse
import sys
import logging
from datetime import datetime, timedelta, date

import os
from database_handler import DatabaseHandler
from data_parser import DataParser, SpecialCases
from calculate_stats import StatCalculator


logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")


def get_db(args):
    """Get database handler with connection."""
    db = DatabaseHandler(args.database)
    db.connect()
    return db


def prices_are_fresh(db, max_age_days=1, update_all=False):
    """
    Check if prices are fresh (updated within max_age_days).
    
    Returns:
    bool: True if prices are fresh, False otherwise
    str: Oldest price date or None if no prices
    """
    cur = db.get_cursor()
    # Get oldest price date depending on whether we check all or only held assets
    condition = "" if update_all else "WHERE amount > 0"
    result = cur.execute(
        f"SELECT MIN(latest_price_date) FROM assets {condition}"
    ).fetchone()
    
    if not result or not result[0]:
        return False, None  # No prices or no assets
    
    oldest_price_date = datetime.strptime(result[0], '%Y-%m-%d').date()
    today = datetime.today().date()
    
    is_fresh = oldest_price_date >= today - timedelta(days=max_age_days)
    return is_fresh, oldest_price_date

def any_assets_need_prices(db, update_all=False):
    """
    Check if any assets have no price date.
    
    Returns:
    bool: True if any assets need prices, False otherwise
    """
    cur = db.get_cursor()
    if update_all:
        query = "SELECT COUNT(*) FROM assets WHERE latest_price_date IS NULL"
    else:
        query = "SELECT COUNT(*) FROM assets WHERE amount > 0 AND latest_price_date IS NULL"
    result = cur.execute(query).fetchone()
    
    return result and result[0] > 0 if result else False


def stats_need_recalculation(db):
    """
    Check if statistics need recalculation.
    
    Stats need recalculation if:
    1. Never calculated before
    2. Transactions processed since last calculation
    3. Prices updated since last calculation
    """
    last_stats = db.get_metadata('last_stats_calculation')
    last_processed = db.get_metadata('last_processed')
    
    if not last_stats:
        return True  # Never calculated
    
    # Check if transactions processed since last calculation
    if last_processed and last_processed > last_stats:
        return True
    
    # Check if prices updated since last calculation
    cur = db.get_cursor()
    result = cur.execute(
        "SELECT MAX(latest_price_date) FROM assets WHERE latest_price_date IS NOT NULL"
    ).fetchone()
    
    if result and result[0]:
        latest_price_date = result[0]
        if latest_price_date > last_stats:
            return True
    
    return False


def import_data(args):
    """Import CSV data and process transactions."""
    db = get_db(args)
    special_cases = SpecialCases(args.special_cases) if args.special_cases else None
    data_parser = DataParser(db, special_cases)
    
    try:
        # Import data
        rows_added = data_parser.add_data(args.file)
        logging.info(f"Added {rows_added} rows to the database")
        
        # Process transactions
        data_parser.process_transactions()
        logging.info("Transactions processed")
        
        # Update metadata
        now = datetime.now().isoformat()
        db.set_metadata('last_import', now)
        db.set_metadata('last_processed', now)
        
        # Clear stats timestamp since we have new data
        db.set_metadata('last_stats_calculation', '')
        
        logging.info("Import completed")
        return 0
        
    except Exception as e:
        logging.error(f"Import failed: {e}")
        return 1











def parse_date_bound(date_str, is_start_bound=False):
    """
    Parse a date string that can be YYYY, YYYY-MM, or YYYY-MM-DD.
    If is_start_bound is True:
        - YYYY resolves to YYYY-01-01
        - YYYY-MM resolves to YYYY-MM-01
    If is_start_bound is False:
        - YYYY resolves to YYYY-12-31
        - YYYY-MM resolves to YYYY-MM-<last_day_of_month>
    """
    if not date_str:
        return None
    
    import calendar
    from datetime import date
    
    date_str = date_str.strip()
    
    # 1. Try YYYY-MM-DD
    try:
        return datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        pass
        
    # 2. Try YYYY-MM
    try:
        dt = datetime.strptime(date_str, "%Y-%m")
        year, month = dt.year, dt.month
        if is_start_bound:
            return date(year, month, 1)
        else:
            last_day = calendar.monthrange(year, month)[1]
            return date(year, month, last_day)
    except ValueError:
        pass
        
    # 3. Try YYYY
    try:
        if len(date_str) == 4 and date_str.isdigit():
            year = int(date_str)
            if is_start_bound:
                return date(year, 1, 1)
            else:
                return date(year, 12, 31)
    except ValueError:
        pass
        
    raise ValueError(f"Invalid date format: '{date_str}'. Supported formats: YYYY, YYYY-MM, YYYY-MM-DD")


def resolve_accounts(db, account_arg):
    """Parse account argument and resolve display names/nicknames to account IDs."""
    account_arg = account_arg.strip()
    if account_arg.lower() == 'all':
        return None
        
    if account_arg.lower() == 'default':
        default_accounts_str = db.get_metadata('default_accounts')
        if not default_accounts_str:
            return None
        raw_accounts = [acc.strip() for acc in default_accounts_str.split(',')]
    else:
        raw_accounts = [acc.strip() for acc in account_arg.split(',')]
        
    # Resolve nicknames to numeric IDs
    nicknames = db.get_all_account_nicknames()
    reverse_nicknames = {v.lower().strip(): k for k, v in nicknames.items()}
    
    resolved = []
    for acc in raw_accounts:
        acc_lower = acc.lower().strip()
        if acc_lower in reverse_nicknames:
            resolved.append(reverse_nicknames[acc_lower])
        else:
            resolved.append(acc)
    return resolved


def check_price_staleness(asset_name, price_date_str, target_date, warnings_list, threshold_days=30):
    if not price_date_str or not target_date:
        return
    try:
        from datetime import datetime, date
        if isinstance(price_date_str, str):
            p_date = datetime.strptime(price_date_str, "%Y-%m-%d").date()
        elif isinstance(price_date_str, (datetime, date)):
            p_date = price_date_str if isinstance(price_date_str, date) else price_date_str.date()
        else:
            return

        if isinstance(target_date, str):
            t_date = datetime.strptime(target_date, "%Y-%m-%d").date()
        elif isinstance(target_date, (datetime, date)):
            t_date = target_date if isinstance(target_date, date) else target_date.date()
        else:
            return

        days_stale = (t_date - p_date).days
        if days_stale > threshold_days:
            warn_msg = f"WARNING: {asset_name} priced from {price_date_str}, {days_stale} days stale for valuation date {t_date.isoformat()}"
            if warn_msg not in warnings_list:
                warnings_list.append(warn_msg)
    except Exception:
        pass


def calc_period_apy(start_val, end_val, period_dep, period_with, days, apy_mode):
    if days <= 0:
        return 0.0
    if apy_mode == 'twrr':
        if start_val > 1e-4 and end_val > 0:
            tr = end_val / start_val
            return 100 * (tr ** (365.0 / days) - 1)
        return 0.0
    else:
        # MWRR / Modified Dietz approximation
        weighted_base = start_val + 0.5 * period_dep - 0.5 * period_with
        gain = end_val - start_val - (period_dep - period_with)
        if weighted_base > 1e-4:
            hpr = gain / weighted_base
            return 100 * hpr * (365.0 / days)
        return 0.0


def get_stats_holdings(db, cohort_month=None, cohorts_start=None, cohorts_end=None, accounts=None):
    cur = db.get_cursor()
    
    # Resolve accounts
    if accounts is None:
        cur.execute("SELECT DISTINCT account FROM cohort_assets")
        accounts = [r[0] for r in cur.fetchall()]
        
    if not accounts:
        return []
        
    placeholders = ",".join("?" for _ in accounts)
    params = list(accounts)
    
    query = f"""
        SELECT a.asset, SUM(ca.amount) AS total_amount, a.latest_price, a.latest_price_date
        FROM cohort_assets ca
        JOIN assets a ON ca.asset_id = a.asset_id
        WHERE ca.account IN ({placeholders}) AND ca.amount > 0.0001
    """
    
    if cohort_month is not None:
        query += " AND ca.month = ?"
        params.append(cohort_month.isoformat() if hasattr(cohort_month, 'isoformat') else str(cohort_month))
    else:
        if cohorts_start is not None:
            query += " AND ca.month >= ?"
            params.append(cohorts_start.isoformat() if hasattr(cohorts_start, 'isoformat') else str(cohorts_start))
        if cohorts_end is not None:
            query += " AND ca.month <= ?"
            params.append(cohorts_end.isoformat() if hasattr(cohorts_end, 'isoformat') else str(cohorts_end))
            
    query += " GROUP BY a.asset HAVING SUM(ca.amount) > 0.0001 ORDER BY (SUM(ca.amount) * COALESCE(a.latest_price, 0.0)) DESC"
    cur.execute(query, params)
    rows = cur.fetchall()
    
    holdings = []
    for asset, amount, price, price_date in rows:
        p_val = price if price is not None else 0.0
        holdings.append({
            'asset': asset,
            'amount': amount,
            'price': p_val,
            'market_value': amount * p_val,
            'price_date': price_date
        })
    return holdings


def is_cohort_older(cohort_month, value_start, period):
    if value_start is None:
        return False
    
    from datetime import date, datetime
    c_year = None
    c_date = None
    
    if hasattr(cohort_month, 'year'):
        c_year = cohort_month.year
        if hasattr(cohort_month, 'month'):
            c_date = date(cohort_month.year, cohort_month.month, cohort_month.day) if hasattr(cohort_month, 'day') else date(cohort_month.year, cohort_month.month, 1)
        else:
            c_date = date(cohort_month.year, 1, 1)
    else:
        try:
            s = str(cohort_month)
            if len(s) == 4:
                c_year = int(s)
                c_date = date(c_year, 1, 1)
            elif len(s) == 7:
                dt = datetime.strptime(s, "%Y-%m")
                c_year = dt.year
                c_date = dt.date()
            else:
                dt = datetime.strptime(s[:10], "%Y-%m-%d")
                c_year = dt.year
                c_date = dt.date()
        except Exception:
            pass
            
    if c_date is None:
        return False
        
    if period == "year":
        return c_year < value_start.year
    else:
        return (c_date.year, c_date.month) < (value_start.year, value_start.month)


def create_temp_snapshot_db(db, target_date, apy_mode, special_cases_path, warnings):
    import shutil
    import os
    import time
    
    # We must ensure target_date is a date object
    if isinstance(target_date, str):
        from datetime import datetime
        t_date = datetime.strptime(target_date, "%Y-%m-%d").date()
    else:
        t_date = target_date
        
    temp_db_path = f"{db.db_file}_temp_as_of_{int(time.time())}_{id(target_date)}.db"
    shutil.copy2(db.db_file, temp_db_path)
    
    temp_db = DatabaseHandler(temp_db_path)
    temp_db.connect()
    
    temp_cur = temp_db.get_cursor()
    temp_cur.execute("DELETE FROM transactions WHERE date > ?", (t_date.isoformat(),))
    
    temp_db.reset_table("cohort_data")
    temp_db.reset_table("cohort_assets")
    temp_db.reset_table("cohort_cash_flows")
    temp_db.reset_table("assets")
    temp_cur.execute("UPDATE transactions SET processed = 0")
    temp_db.commit()
    
    special_cases = SpecialCases(special_cases_path) if special_cases_path else None
    parser = DataParser(temp_db, special_cases)
    parser.process_transactions()
    
    # Resolve prices
    held_assets_rows = temp_cur.execute("SELECT DISTINCT asset_id FROM cohort_assets WHERE amount > 0.001").fetchall()
    held_asset_ids = {row[0] for row in held_assets_rows}
    
    assets = temp_cur.execute("SELECT asset_id, asset FROM assets").fetchall()
    for asset_id, asset_name in assets:
        price_row = temp_cur.execute("""
            SELECT price, price_date FROM asset_prices
            WHERE asset_id = ? AND price_date <= ?
            ORDER BY price_date DESC LIMIT 1
        """, (asset_id, t_date.isoformat())).fetchone()
        if price_row:
            temp_cur.execute("""
                UPDATE assets
                SET latest_price = ?, latest_price_date = ?
                WHERE asset_id = ?
            """, (price_row[0], price_row[1], asset_id))
            if asset_id in held_asset_ids:
                check_price_staleness(asset_name, price_row[1], t_date, warnings)
    temp_db.commit()
    
    stat_calc = StatCalculator(temp_db)
    stat_calc.calculate_cohort_stats(apy_mode=apy_mode, today=t_date)
    stat_calc.calculate_year_stats(apy_mode=apy_mode, today=t_date)
    
    return temp_db, temp_db_path


def stats(args):
    """Smart statistics command with automatic updates."""
    db = get_db(args)
    
    # Parse account filter
    accounts = resolve_accounts(db, args.account)
    
    as_of = None
    cohorts_start = None
    cohorts_end = None
    value_start = None
    value_end = None
    
    try:
        as_of = parse_date_bound(getattr(args, 'as_of', None), is_start_bound=False)
        
        cohort_raw = getattr(args, 'cohort', None)
        if cohort_raw is not None:
            c_start_raw = getattr(args, 'cohorts_start', None)
            c_end_raw = getattr(args, 'cohorts_end', None)
            if c_start_raw is not None or c_end_raw is not None:
                logging.error("Cannot specify both --cohort and --cohorts-start/--cohorts-end")
                return 1
                
            import re
            if re.match(r"^\d{4}-\d{2}$", cohort_raw):
                c_start_raw = cohort_raw
                c_end_raw = cohort_raw
                if getattr(args, 'period', 'default') == 'default':
                    args.period = 'month'
            elif re.match(r"^\d{4}$", cohort_raw):
                c_start_raw = f"{cohort_raw}-01"
                c_end_raw = f"{cohort_raw}-12"
                if getattr(args, 'period', 'default') == 'default':
                    args.period = 'year'
            else:
                logging.error(f"Invalid --cohort format: '{cohort_raw}'. Must be YYYY-MM or YYYY.")
                return 1
        else:
            c_start_raw = getattr(args, 'cohorts_start', None)
            c_end_raw = getattr(args, 'cohorts_end', None)
            
        v_start_raw = getattr(args, 'from_date', None)
        v_end_raw = getattr(args, 'to', None)
        
        if v_end_raw is None and as_of is not None:
            v_end_raw = getattr(args, 'as_of', None)
            
        cohorts_start = parse_date_bound(c_start_raw, is_start_bound=True)
        cohorts_end = parse_date_bound(c_end_raw, is_start_bound=False)
        value_start = parse_date_bound(v_start_raw, is_start_bound=True)
        value_end = parse_date_bound(v_end_raw, is_start_bound=False)
        
    except ValueError as e:
        logging.error(str(e))
        return 1

    # Date filters trigger temp DB snapshotting
    has_date_filters = (as_of is not None or value_start is not None or value_end is not None)

    # Update prices if needed (only if not running past historical query or if force)
    update_all = getattr(args, 'update_all', False)
    if args.update_prices == 'always':
        # Force price update
        fresh, oldest_date = prices_are_fresh(db, update_all=update_all)
        if fresh:
            logging.info(f"Prices are already fresh (oldest: {oldest_date}), updating anyway...")
        try:
            stat_calc = StatCalculator(db)
            stat_calc.update_prices(force=True, update_all=update_all)
            
            # Update metadata
            now = datetime.now().isoformat()
            db.set_metadata('last_price_update', now)
            
            # Clear stats timestamp since prices changed
            db.set_metadata('last_stats_calculation', '')
            
            logging.info("Prices updated successfully")
        except Exception as e:
            logging.error(f"Failed to update prices: {e}")
            return 1
            
    elif args.update_prices == 'auto' and not has_date_filters:
        fresh, oldest_date = prices_are_fresh(db, update_all=update_all)
        need_prices = any_assets_need_prices(db, update_all=update_all)
        
        if not fresh or need_prices:
            if need_prices:
                logging.info("Some assets have no prices, updating...")
            else:
                logging.info(f"Prices are stale (oldest: {oldest_date}), updating...")
            
            try:
                stat_calc = StatCalculator(db)
                stat_calc.update_prices(force=True, update_all=update_all)
                
                # Update metadata
                now = datetime.now().isoformat()
                db.set_metadata('last_price_update', now)
                
                # Clear stats timestamp since prices changed
                db.set_metadata('last_stats_calculation', '')
                
                logging.info("Prices updated successfully")
            except Exception as e:
                logging.error(f"Failed to update prices: {e}")
                return 1
    
    # Calculate stats if needed
    apy_mode = getattr(args, 'apy_mode', 'mwrr')
    # Force recalculation if APY mode changed
    last_apy_mode = db.get_metadata('last_apy_mode') or 'mwrr'
    if last_apy_mode == 'modified-dietz':
        last_apy_mode = 'mwrr'
    apy_mode_changed = (apy_mode != last_apy_mode)
    if not has_date_filters and (args.force or apy_mode_changed or stats_need_recalculation(db)):
        try:
            stat_calc = StatCalculator(db)
            stat_calc.calculate_cohort_stats(apy_mode=apy_mode)
            stat_calc.calculate_year_stats(apy_mode=apy_mode)
            
            # Update metadata
            now = datetime.now().isoformat()
            db.set_metadata('last_stats_calculation', now)
            db.set_metadata('last_apy_mode', apy_mode)
            
            logging.info("Statistics calculated")
        except Exception as e:
            logging.error(f"Failed to calculate statistics: {e}")
            return 1
    
    # Display statistics
    start_temp_db = None
    start_temp_db_path = None
    end_temp_db = None
    end_temp_db_path = None
    
    try:
        # Determine effective end target date
        target_end_date = value_end if value_end is not None else as_of
        if target_end_date is None and (value_start is not None or cohorts_start is not None or cohorts_end is not None):
            target_end_date = date.today()
            
        warnings = []
        if value_start is not None:
            # Double snapshot calculation
            start_temp_db, start_temp_db_path = create_temp_snapshot_db(db, value_start, apy_mode, getattr(args, 'special_cases', None), warnings)
            end_temp_db, end_temp_db_path = create_temp_snapshot_db(db, target_end_date, apy_mode, getattr(args, 'special_cases', None), warnings)
            active_db = end_temp_db
        elif target_end_date is not None:
            # Single snapshot calculation
            end_temp_db, end_temp_db_path = create_temp_snapshot_db(db, target_end_date, apy_mode, getattr(args, 'special_cases', None), warnings)
            active_db = end_temp_db
        else:
            # Main database
            active_db = db
            
        stat_calc = StatCalculator(active_db)
        period = args.period
        if period == 'default':
            period = active_db.get_metadata('default_stats_period') or 'month'

        kwargs = {
            'period': period,
            'deposits': args.deposits,
            'apy_mode': apy_mode
        }
        
        # Add account filter if specified
        if accounts is not None:
            kwargs['accounts'] = accounts
            
        # Get start/end maps for delta calculation
        start_map = {}
        if value_start is not None:
            start_calc = StatCalculator(start_temp_db)
            start_stats = start_calc.get_stats(**kwargs)
            
            def get_key(d):
                if hasattr(d, 'isoformat'):
                    return d.isoformat()
                return str(d)
                
            start_map = { get_key(r[0]): r for r in start_stats }
            
            end_calc = StatCalculator(end_temp_db)
            end_stats = end_calc.get_stats(**kwargs)
            
            stats_list = []
            days = (target_end_date - value_start).days
            for end_row in end_stats:
                date_key = get_key(end_row[0])
                if date_key in start_map:
                    start_row = start_map[date_key]
                    
                    p_dep = end_row[1] - start_row[1]
                    p_with = end_row[2] - start_row[2]
                    p_val = end_row[3]
                    p_gain = end_row[4] - start_row[4]
                    p_real = end_row[5] - start_row[5]
                    p_unreal = end_row[6] - start_row[6]
                    
                    base_cap = start_row[3] + p_dep
                    if base_cap > 1e-4:
                        p_gain_pct = 100.0 * p_gain / base_cap
                        p_real_pct = 100.0 * p_real / base_cap
                        p_unreal_pct = 100.0 * p_unreal / base_cap
                    else:
                        p_gain_pct = p_real_pct = p_unreal_pct = 0.0
                        
                    p_apy = calc_period_apy(start_row[3], p_val, p_dep, p_with, days, apy_mode)
                    
                    stats_list.append((
                        end_row[0], p_dep, p_with, p_val, p_gain, p_real, p_unreal,
                        p_gain_pct, p_real_pct, p_unreal_pct, p_apy, start_row[3]
                    ))
                else:
                    # Cohort didn't exist at value_start
                    stats_list.append(end_row + (0.0,))
        else:
            stats_list = stat_calc.get_stats(**kwargs)
            
        # Filter by deposits parameter for double snapshot
        if value_start is not None and args.deposits == "current":
            stats_list = [row for row in stats_list if row[3] > 0 or (get_key(row[0]) in start_map and start_map[get_key(row[0])][3] > 0)]

        # Apply cohorts range filtering
        filtered_stats = []
        for row in stats_list:
            row_date = row[0]
            if isinstance(row_date, str):
                if '-' in row_date:
                    rd = datetime.strptime(row_date, "%Y-%m-%d").date()
                else:
                    rd = date(int(row_date), 12, 31)
            else:
                rd = row_date
                
            if cohorts_start is not None and rd < cohorts_start:
                continue
            if cohorts_end is not None and rd > cohorts_end:
                continue
            filtered_stats.append(row)
        stats_list = filtered_stats
        
        # Output summary mode or standard list mode
        if getattr(args, 'summary', False):
            total_dep = sum(row[1] for row in stats_list)
            total_with = sum(row[2] for row in stats_list)
            total_val = sum(row[3] for row in stats_list)
            total_gain = sum(row[4] for row in stats_list)
            total_real = sum(row[5] for row in stats_list)
            total_unreal = sum(row[6] for row in stats_list)
            
            start_val = 0.0
            if value_start is not None:
                start_val = sum(start_map[get_key(row[0])][3] for row in stats_list if get_key(row[0]) in start_map)
                days = (target_end_date - value_start).days
                blended_apy = calc_period_apy(start_val, total_val, total_dep, total_with, days, apy_mode)
            else:
                acc_list = accounts if accounts is not None else 'all'
                blended_apy, _ = stat_calc.calculate_account_apy(acc_list, apy_mode=apy_mode, end_date=target_end_date, current_value=total_val)
                
            holdings = []
            if getattr(args, 'positions', False):
                holdings = get_stats_holdings(active_db, cohorts_start=cohorts_start, cohorts_end=cohorts_end, accounts=accounts)
                for h in holdings:
                    check_price_staleness(h['asset'], h['price_date'], target_end_date or date.today(), warnings)
                    
            if getattr(args, 'format', 'table') == 'json':
                import json
                net_transfers = total_val + total_with - start_val - total_dep - total_gain
                result = {
                    'period': f"{value_start.isoformat()} to {target_end_date.isoformat()}" if value_start else "all",
                    'deposits': total_dep,
                    'withdrawals': total_with,
                    'current_value': total_val,
                    'total_gain': total_gain,
                    'total_gain_percent': (total_gain / (start_val + total_dep) * 100) if ((start_val + total_dep) > 0) else 0.0,
                    'apy': blended_apy
                }
                if abs(net_transfers) > 0.01:
                    result['net_transfers'] = net_transfers
                if value_start is not None:
                    result['start_value'] = start_val
                if accounts is not None and len(accounts) == 1:
                    acc_id = list(accounts)[0]
                    nicknames = db.get_all_account_nicknames()
                    result['account'] = acc_id
                    result['display_name'] = nicknames.get(acc_id, acc_id)
                if getattr(args, 'positions', False):
                    total_assets_val = sum(h['market_value'] for h in holdings)
                    result['holdings'] = [
                        {
                            'asset': h['asset'],
                            'amount': h['amount'],
                            'price': h['price'],
                            'market_value': h['market_value'],
                            'allocation_percent': (h['market_value'] / total_assets_val * 100) if total_assets_val > 0 else 0.0
                        }
                        for h in holdings
                    ]
                print(json.dumps(result, indent=2))
            else:
                is_portfolio = getattr(args, 'is_portfolio', False)
                if is_portfolio and accounts is not None and len(accounts) == 1:
                    account_id = list(accounts)[0]
                    nicknames = db.get_all_account_nicknames()
                    nickname = nicknames.get(account_id)
                    account_label = f"Account {account_id}"
                    if nickname:
                        account_label += f" ({nickname})"
                else:
                    account_label = None
                    
                period_label = f"{value_start.isoformat()} to {target_end_date.isoformat()}" if value_start else "all cohorts"
                if account_label:
                    header_title = f"{account_label} — {period_label}" if value_start else account_label
                else:
                    header_title = f"Summary — {period_label}"
                print(f"{header_title}\n")
                net_transfers = total_val + total_with - start_val - total_dep - total_gain
                if value_start is not None:
                    print(f"Start Value:       {start_val:,.0f} SEK")
                print(f"Total Deposited:   {total_dep:,.0f} SEK")
                print(f"Total Withdrawn:   {total_with:,.0f} SEK")
                if abs(net_transfers) > 0.01:
                    trans_sign = "+" if net_transfers > 0 else ""
                    print(f"Net Transfers:     {trans_sign}{net_transfers:,.0f} SEK")
                print(f"Current Value:     {total_val:,.0f} SEK")
                
                gl_sign = "+" if total_gain > 0 else ""
                gain_pct = (total_gain / (start_val + total_dep) * 100) if ((start_val + total_dep) > 0) else 0.0
                gl_pct_sign = "+" if gain_pct > 0 else ""
                print(f"Total Gain:       {gl_sign}{total_gain:,.0f} SEK ({gl_pct_sign}{gain_pct:.1f}%)")
                
                apy_str = f"{blended_apy:.1f}%" if blended_apy is not None else "N/A"
                print(f"Blended APY:       {apy_str} ({apy_mode.upper()})")
                
                if getattr(args, 'positions', False):
                    print()
                    print("Holdings:")
                    if holdings:
                        name_w = max(max(len(h['asset']) for h in holdings), 9)
                        print(f"  {'Fund Name':<{name_w}} {'Market Value':>15} {'Allocation':>11}")
                        total_assets_val = sum(h['market_value'] for h in holdings)
                        for h in holdings:
                            alloc = (h['market_value'] / total_assets_val * 100) if total_assets_val > 0 else 0.0
                            print(f"  {h['asset']:<{name_w}} {h['market_value']:>15,.0f} SEK {alloc:>10.1f}%")
                        print(f"  {'Total':<{name_w}} {total_assets_val:>15,.0f} SEK {'100.0%':>11}")
                    else:
                        print("  None")
        else:
            # Cohort-level breakdown mode
            if getattr(args, 'format', 'table') == 'json':
                import json
                def serialize_date(d):
                    if hasattr(d, 'isoformat'):
                        return d.isoformat()
                    return str(d)
                json_data = []
                for row in stats_list:
                    if row[1] > 0 or row[3] > 0:
                        cohort_month = row[0]
                        cohort_data = {
                            'date': serialize_date(cohort_month),
                            'withdrawal': row[2],
                            'value': row[3],
                            'total_gainloss': row[4],
                            'total_gainloss_percent': row[7],
                            'realized_gainloss': row[5],
                            'realized_gainloss_percent': row[8],
                            'unrealized_gainloss': row[6],
                            'unrealized_gainloss_percent': row[9],
                            'apy': row[10]
                        }
                        if value_start is not None and is_cohort_older(cohort_month, value_start, period):
                            start_val = row[11] if len(row) > 11 else 0.0
                            cohort_data['start_value'] = start_val
                        else:
                            cohort_data['deposit'] = row[1]
                        if getattr(args, 'positions', False):
                            cohort_holdings = get_stats_holdings(active_db, cohort_month=cohort_month, accounts=accounts)
                            for h in cohort_holdings:
                                check_price_staleness(h['asset'], h['price_date'], target_end_date or date.today(), warnings)
                            total_assets_val = sum(h['market_value'] for h in cohort_holdings)
                            cohort_data['holdings'] = [
                                {
                                    'asset': h['asset'],
                                    'amount': h['amount'],
                                    'price': h['price'],
                                    'market_value': h['market_value'],
                                    'allocation_percent': (h['market_value'] / total_assets_val * 100) if total_assets_val > 0 else 0.0
                                }
                                for h in cohort_holdings
                            ]
                        json_data.append(cohort_data)
                print(json.dumps(json_data, indent=2))
            else:
                for row in stats_list:
                    if row[1] > 0 or row[3] > 0:
                        cohort_month = row[0]
                        if hasattr(cohort_month, 'year'):
                            if period == "year":
                                display_date = cohort_month.year
                            else:
                                display_date = cohort_month.strftime("%b %Y")
                        else:
                            display_date = str(cohort_month)
                            
                        print(display_date)
                        if value_start is not None and is_cohort_older(cohort_month, value_start, period):
                            start_val = row[11] if len(row) > 11 else 0.0
                            print(f"Start Value: {start_val:.0f}")
                        else:
                            print(f"Deposited: {row[1]:.0f}")
                        print(f"Value: {row[3]:.0f}")
                        print(f"Withdrawal: {row[2]:.0f}")
                        gl_sign = "+" if row[4] > 0 else ""
                        gl_pct_sign = "+" if row[7] > 0 else ""
                        print(f"Gain/Loss: {gl_sign}{row[4]:.0f} ({gl_pct_sign}{row[7]:.1f}%)")
                        un_sign = "+" if row[6] > 0 else ""
                        un_pct_sign = "+" if row[9] > 0 else ""
                        print(f"- Unrealized: {un_sign}{row[6]:.0f} ({un_pct_sign}{row[9]:.1f}%)")
                        re_sign = "+" if row[5] > 0 else ""
                        re_pct_sign = "+" if row[8] > 0 else ""
                        print(f"- Realized: {re_sign}{row[5]:.0f} ({re_pct_sign}{row[8]:.1f}%)")
                        apy_str = f"{row[10]:.1f}%" if row[10] is not None else "N/A"
                        print(f"APY: {apy_str}")
                        
                        if getattr(args, 'positions', False):
                            cohort_holdings = get_stats_holdings(active_db, cohort_month=cohort_month, accounts=accounts)
                            for h in cohort_holdings:
                                check_price_staleness(h['asset'], h['price_date'], target_end_date or date.today(), warnings)
                            if cohort_holdings:
                                print("  Holdings:")
                                name_w = max(max(len(h['asset']) for h in cohort_holdings), 9)
                                print(f"    {'Fund Name':<{name_w}} {'Market Value':>15} {'Allocation':>11}")
                                total_assets_val = sum(h['market_value'] for h in cohort_holdings)
                                for h in cohort_holdings:
                                    alloc = (h['market_value'] / total_assets_val * 100) if total_assets_val > 0 else 0.0
                                    print(f"    {h['asset']:<{name_w}} {h['market_value']:>15,.0f} SEK {alloc:>10.1f}%")
                            else:
                                print("  Holdings: None")
                        print()
                        
        if warnings and not getattr(args, 'quiet', False):
            import sys
            for warning in warnings:
                print(warning, file=sys.stderr)
                
        return 0
        
    except Exception as e:
        logging.error(f"Failed to show statistics: {e}")
        return 1
    finally:
        for t_db, t_path in [(start_temp_db, start_temp_db_path), (end_temp_db, end_temp_db_path)]:
            if t_db is not None:
                t_db.disconnect()
                import gc
                gc.collect()
                if t_path and os.path.exists(t_path):
                    try:
                        os.remove(t_path)
                    except Exception as e:
                        logging.warning(f"Failed to remove temp db file: {e}")


def status(args):
    """Show system status."""
    db = get_db(args)
    
    print("=== Investment Tracker Status ===")
    
    # Database stats
    stats_list = ["Transactions", "Processed", "Unprocessed", "Assets", "Capital"]
    db_stats = db.get_db_stats(stats_list)
    
    print(f"\nDatabase:")
    print(f"  Transactions: {db_stats.get('Transactions', 0)}")
    print(f"  Processed: {db_stats.get('Processed', 0)}")
    print(f"  Unprocessed: {db_stats.get('Unprocessed', 0)}")
    print(f"  Assets: {db_stats.get('Assets', 0)}")
    print(f"  Capital: {db_stats.get('Capital', 0):.0f} SEK")
    
    min_date, max_date = db.get_date_range()
    if min_date and max_date:
        print(f"  Date range: {min_date} to {max_date}")
    elif min_date or max_date:
        print(f"  Date range: {min_date or max_date}")
    
    # Price freshness
    fresh, oldest_date = prices_are_fresh(db)
    price_status = "Fresh" if fresh else "Stale"
    print(f"\nPrices:")
    print(f"  Status: {price_status}")
    if oldest_date:
        print(f"  Oldest price date: {oldest_date}")
    
    # Metadata
    metadata = db.get_all_metadata()
    print(f"\nMetadata:")
    for key in ['last_import', 'last_processed', 'last_price_update', 'last_stats_calculation']:
        value = metadata.get(key, 'Never')
        print(f"  {key}: {value}")
    
    # Stats freshness
    needs_recalc = stats_need_recalculation(db)
    print(f"\nStatistics:")
    print(f"  Need recalculation: {'Yes' if needs_recalc else 'No'}")
    
    return 0


def reset(args):
    """Reset database state."""
    db = get_db(args)
    special_cases = SpecialCases(args.special_cases) if args.special_cases else None
    data_parser = DataParser(db, special_cases)
    
    try:
        if getattr(args, 'hard', False):
            # Hard reset: delete all transaction and calculation tables
            tables_to_reset = [
                "transactions", "cohort_data", "cohort_assets", "assets",
                "cohort_cash_flows", "asset_prices", "cohort_stats", "year_stats"
            ]
            for t in ["account_cohort_stats", "account_year_stats"]:
                if t in db.tables:
                    tables_to_reset.append(t)
            
            for table in tables_to_reset:
                db.reset_table(table)
            
            logging.info("Database hard reset successfully (deleted all transaction and calculation tables)")
        else:
            data_parser.reset_processed_transactions()
            logging.info("Database reset successfully")
        
        # Clear metadata
        for key in ['last_processed', 'last_stats_calculation']:
            db.set_metadata(key, '')
        
        return 0
        
    except Exception as e:
        logging.error(f"Failed to reset database: {e}")
        return 1


def settings_default_stats_period(args):
    """Set default period for stats command."""
    db = get_db(args)
    
    period = args.period.strip().lower()
    if period in ('month', 'year'):
        db.set_metadata('default_stats_period', period)
        logging.info(f"Default stats period set to '{period}'")
    else:
        logging.error("Invalid period: must be 'month' or 'year'")
        return 1

def settings_default_accounts(args):
    """Set default accounts for filtering."""
    db = get_db(args)
    
    accounts = args.accounts.strip()
    if accounts.lower() == 'all':
        # Store empty string to indicate all accounts
        db.set_metadata('default_accounts', '')
        logging.info("Default accounts set to 'all' (all accounts)")
    else:
        # Validate comma-separated list
        account_list = [acc.strip() for acc in accounts.split(',')]
        if not all(acc for acc in account_list):
            logging.error("Invalid account list: empty account found")
            return 1
        
        # Store as comma-separated string
        db.set_metadata('default_accounts', ','.join(account_list))
        logging.info(f"Default accounts set to: {', '.join(account_list)}")
    
    return 0


def settings_account_nickname(args):
    """Set or remove account nicknames."""
    db = get_db(args)
    
    if args.remove:
        # Remove nickname for specified account
        account = args.remove.strip()
        if db.remove_account_nickname(account):
            logging.info(f"Removed nickname for account '{account}'")
        else:
            logging.info(f"No nickname set for account '{account}'")
    elif args.list:
        # List all nicknames
        nicknames = db.get_all_account_nicknames()
        if nicknames:
            print("Account nicknames:")
            for account, nickname in sorted(nicknames.items()):
                print(f"  {account}: {nickname}")
        else:
            print("No account nicknames set")
    elif args.account and args.nickname is not None:
        # Set nickname
        account = args.account.strip()
        nickname = args.nickname.strip()
        db.set_account_nickname(account, nickname)
        logging.info(f"Set nickname for account '{account}' to '{nickname}'")
    else:
        logging.error("Must specify --remove, --list, or both account and nickname")
        return 1
    
    return 0


def accounts_summary(args):
    """Show account summaries with asset values and cash."""
    db = get_db(args)
    
    # Parse account filter (same logic as stats)
    accounts = resolve_accounts(db, args.account)
    
    # Update prices if needed (same logic as stats)
    update_all = getattr(args, 'update_all', False)
    if args.update_prices == 'always':
        # Force price update
        fresh, oldest_date = prices_are_fresh(db, update_all=update_all)
        if fresh:
            logging.info(f"Prices are already fresh (oldest: {oldest_date}), updating anyway...")
        try:
            stat_calc = StatCalculator(db)
            stat_calc.update_prices(force=True, update_all=update_all)
            
            # Update metadata
            now = datetime.now().isoformat()
            db.set_metadata('last_price_update', now)
            
            logging.info("Prices updated successfully")
        except Exception as e:
            logging.error(f"Failed to update prices: {e}")
            return 1
            
    elif args.update_prices == 'auto':
        fresh, oldest_date = prices_are_fresh(db, update_all=update_all)
        need_prices = any_assets_need_prices(db, update_all=update_all)
        
        if not fresh or need_prices:
            if need_prices:
                logging.info("Some assets have no prices, updating...")
            else:
                logging.info(f"Prices are stale (oldest: {oldest_date}), updating...")
            
            try:
                stat_calc = StatCalculator(db)
                stat_calc.update_prices(force=True, update_all=update_all)
                
                # Update metadata
                now = datetime.now().isoformat()
                db.set_metadata('last_price_update', now)
                
                logging.info("Prices updated successfully")
            except Exception as e:
                logging.error(f"Failed to update prices: {e}")
                return 1
    
    # Display account summary
    try:
        stat_calc = StatCalculator(db)
        if getattr(args, 'format', 'table') == 'json':
            summaries = stat_calc.get_account_summaries(accounts)
            nicknames = db.get_all_account_nicknames()
            json_data = []
            for account, cash, asset_value, total in summaries:
                json_data.append({
                    'account': account,
                    'display_name': nicknames.get(account, account),
                    'cash': cash,
                    'assets': asset_value,
                    'total': total
                })
            import json
            print(json.dumps(json_data, indent=2))
        else:
            stat_calc.print_account_summary(accounts=accounts)
        return 0
        
    except Exception as e:
        logging.error(f"Failed to show account summary: {e}")
        return 1

    return 0


def portfolio(args):
    """Show portfolio snapshot as an alias to stats --positions --summary."""
    fmt = getattr(args, 'format', 'table')
    if fmt not in ('table', 'json'):
        fmt = 'table'
        
    stats_args = argparse.Namespace(
        database=args.database,
        special_cases=getattr(args, 'special_cases', None),
        account=args.account,
        apy_mode=getattr(args, 'apy_mode', 'mwrr'),
        as_of=getattr(args, 'as_of', None),
        cohorts_start=getattr(args, 'cohorts_start', None),
        cohorts_end=getattr(args, 'cohorts_end', None),
        cohort=getattr(args, 'cohort', None),
        from_date=getattr(args, 'from_date', None),
        to=getattr(args, 'to', None),
        positions=True,
        summary=True,
        is_portfolio=True,
        format=fmt,
        quiet=getattr(args, 'quiet', False),
        period='default',
        deposits='current',
        accumulated=False,
        update_prices='never',
        update_all=False,
        force=False
    )
    return stats(stats_args)


def export_transactions(args):
    import os
    db = get_db(args)
    cur = db.get_cursor()
    
    # Resolve accounts
    accounts = resolve_accounts(db, args.account)
    
    # Query transactions
    query = "SELECT date, account, transaction_type, asset_name, amount, price, total, courtage, currency, isin FROM transactions"
    params = []
    if accounts is not None:
        placeholders = ','.join('?' for _ in accounts)
        query += f" WHERE account IN ({placeholders})"
        params.extend(accounts)
    query += " ORDER BY date ASC, rowid ASC"
    
    cur.execute(query, params)
    rows = cur.fetchall()
    
    # CSV Header
    header = "Datum;Konto;Typ av transaktion;Värdepapper/beskrivning;Antal;Kurs;Belopp;Courtage;Valuta;ISIN;Resultat"
    
    csv_lines = [header]
    
    def format_cell(val, is_numeric=False):
        if val is None:
            return "-" if is_numeric else ""
        if is_numeric:
            if val == 0 or val == 0.0:
                return "-"
            if isinstance(val, float) and val.is_integer():
                val = int(val)
            return str(val).replace('.', ',')
        return str(val)
        
    for row in rows:
        cells = [
            format_cell(row[0]),  # Datum
            format_cell(row[1]),  # Konto
            format_cell(row[2]),  # Typ av transaktion
            format_cell(row[3]),  # Värdepapper/beskrivning
            format_cell(row[4], is_numeric=True),  # Antal
            format_cell(row[5], is_numeric=True),  # Kurs
            format_cell(row[6], is_numeric=True),  # Belopp
            format_cell(row[7], is_numeric=True),  # Courtage
            format_cell(row[8]),  # Valuta
            format_cell(row[9]),  # ISIN
            "-"                   # Resultat
        ]
        csv_lines.append(";".join(cells))
        
    content = "\n".join(csv_lines) + "\n"
    
    if args.output == '-':
        print(content, end='')
    else:
        # Ensure parent directory exists
        out_path = args.output
        parent_dir = os.path.dirname(out_path)
        if parent_dir and not os.path.exists(parent_dir):
            os.makedirs(parent_dir)
            
        with open(out_path, 'w', encoding='utf-8') as f:
            f.write(content)
        logging.info(f"Successfully exported {len(rows)} transactions to '{out_path}'")
        
    return 0


def main():
    parser = argparse.ArgumentParser(
        description="Avanza investment tracker CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s import transactions.csv
  %(prog)s stats --update-prices auto
  %(prog)s status
        """
    )
    
    parser.add_argument(
        '--database',
        default='data/asset_data.db',
        help='Path to SQLite database (default: data/asset_data.db)'
    )
    parser.add_argument(
        '--special-cases',
        default='data/special_cases.json',
        help='Path to special cases JSON file (default: data/special_cases.json)'
    )
    
    subparsers = parser.add_subparsers(dest='command', help='Command to execute')
    
    # Import command
    import_parser = subparsers.add_parser('import', help='Import CSV data and process transactions')
    import_parser.add_argument('file', help='Path to CSV file')
    import_parser.set_defaults(func=import_data)
    
    # Stats command
    stats_parser = subparsers.add_parser('stats', help='Show statistics with smart updates')
    stats_parser.add_argument(
        '--period',
        choices=['default', 'month', 'year'],
        default='default',
        help='Time period to show (default)'
    )
    stats_parser.add_argument(
        '--deposits',
        choices=['current', 'all'],
        default='current',
        help='Which deposits to include (default: current)'
    )
    stats_parser.add_argument(
        '--accumulated',
        action='store_true',
        help='Show accumulated statistics'
    )
    stats_parser.add_argument(
        '--update-prices',
        choices=['auto', 'always', 'never'],
        default='auto',
        help='When to update prices (default: auto)'
    )
    stats_parser.add_argument(
        '--update-all',
        action='store_true',
        help='Update all assets in database regardless of whether they are currently held'
    )
    stats_parser.add_argument(
        '--force',
        action='store_true',
        help='Force statistics recalculation'
    )
    stats_parser.add_argument(
        '--account',
        default='all',
        help='Accounts to include: "default", "all", or comma-separated list of accounts'
    )
    stats_parser.add_argument(
        '--apy-mode',
        choices=['mwrr', 'twrr'],
        default='mwrr',
        help='APY calculation method (default: mwrr)'
    )
    stats_parser.add_argument(
        '--as-of',
        default=None,
        help='Show statistics as of a previous date (formats: YYYY, YYYY-MM, YYYY-MM-DD)'
    )
    stats_parser.add_argument(
        '--cohorts-start',
        default=None,
        help='Start date for filtering cohorts by creation date (formats: YYYY, YYYY-MM, YYYY-MM-DD)'
    )
    stats_parser.add_argument(
        '--cohorts-end',
        default=None,
        help='End date for filtering cohorts by creation date (formats: YYYY, YYYY-MM, YYYY-MM-DD)'
    )
    stats_parser.add_argument(
        '--cohort',
        default=None,
        help='Shorthand to filter by a single cohort month (YYYY-MM) or year (YYYY) (default: None)'
    )
    stats_parser.add_argument(
        '--from',
        dest='from_date',
        default=None,
        help='Start date for valuation performance period (formats: YYYY, YYYY-MM, YYYY-MM-DD)'
    )
    stats_parser.add_argument(
        '--to',
        default=None,
        help='End date for valuation performance period (formats: YYYY, YYYY-MM, YYYY-MM-DD)'
    )
    stats_parser.add_argument(
        '--positions', '-p',
        action='store_true',
        help='Show asset positions/holdings for each cohort'
    )
    stats_parser.add_argument(
        '--summary', '-s',
        action='store_true',
        help='Consolidate cohort stats and positions into a single overview'
    )
    stats_parser.add_argument(
        '--format',
        choices=['table', 'json'],
        default='table',
        help='Output format (default: table)'
    )
    stats_parser.add_argument(
        '--quiet', '-q',
        action='store_true',
        help='Suppress price data staleness warnings'
    )
    stats_parser.set_defaults(func=stats)
    
    # Settings command
    settings_parser = subparsers.add_parser('settings', help='Manage settings')
    settings_subparsers = settings_parser.add_subparsers(dest='settings_command', help='Settings command')
    
    # Default stats period subcommand
    default_stats_period_parser = settings_subparsers.add_parser('default-stats-period', help='Set default stats period')
    default_stats_period_parser.add_argument('period', choices=['month', 'year'], help='Default period: "month" or "year"')
    default_stats_period_parser.set_defaults(func=settings_default_stats_period)

    # Default accounts subcommand
    default_accounts_parser = settings_subparsers.add_parser('default-accounts', help='Set default accounts')
    default_accounts_parser.add_argument('accounts', help='Comma-separated list of account numbers, or "all" for all accounts')
    default_accounts_parser.set_defaults(func=settings_default_accounts)
    
    # Account nickname subcommand
    account_nickname_parser = settings_subparsers.add_parser('account-nickname', help='Set or remove account nicknames')
    account_nickname_parser.add_argument('account', nargs='?', help='Account number to set nickname for')
    account_nickname_parser.add_argument('nickname', nargs='?', help='Nickname for the account')
    account_nickname_parser.add_argument('--remove', metavar='ACCOUNT', help='Remove nickname for specified account')
    account_nickname_parser.add_argument('--list', action='store_true', help='List all account nicknames')
    account_nickname_parser.set_defaults(func=settings_account_nickname)
    
    # Accounts command (show account summaries)
    accounts_parser = subparsers.add_parser('accounts', help='Show account summaries with asset values and cash')
    accounts_parser.add_argument(
        '--account',
        default='all',
        help='Accounts to include: "default", "all", or comma-separated list of accounts'
    )
    accounts_parser.add_argument(
        '--update-prices',
        choices=['auto', 'always', 'never'],
        default='auto',
        help='When to update prices (default: auto)'
    )
    accounts_parser.add_argument(
        '--update-all',
        action='store_true',
        help='Update all assets in database regardless of whether they are currently held'
    )
    accounts_parser.add_argument(
        '--format',
        choices=['table', 'json'],
        default='table',
        help='Output format (default: table)'
    )
    accounts_parser.set_defaults(func=accounts_summary)
    
    # Portfolio command (show portfolio snapshot)
    portfolio_parser = subparsers.add_parser('portfolio', help='Show portfolio snapshot')
    portfolio_parser.add_argument(
        '--as-of',
        default=None,
        help='Valuation date (YYYY, YYYY-MM, YYYY-MM-DD)'
    )
    portfolio_parser.add_argument(
        '--cohorts-start',
        default=None,
        help='Start date for filtering cohorts by creation date (formats: YYYY, YYYY-MM, YYYY-MM-DD)'
    )
    portfolio_parser.add_argument(
        '--cohorts-end',
        default=None,
        help='End date for filtering cohorts by creation date (formats: YYYY, YYYY-MM, YYYY-MM-DD)'
    )
    portfolio_parser.add_argument(
        '--cohort',
        default=None,
        help='Shorthand to filter by a single cohort month (YYYY-MM) or year (YYYY) (default: None)'
    )
    portfolio_parser.add_argument(
        '--from',
        dest='from_date',
        default=None,
        help='Start date for valuation performance period (formats: YYYY, YYYY-MM, YYYY-MM-DD)'
    )
    portfolio_parser.add_argument(
        '--to',
        default=None,
        help='End date for valuation performance period (formats: YYYY, YYYY-MM, YYYY-MM-DD)'
    )
    portfolio_parser.add_argument(
        '--account',
        default='all',
        help='Accounts to include: "default", "all", or comma-separated list of accounts'
    )
    portfolio_parser.add_argument(
        '--apy-mode',
        choices=['mwrr', 'twrr'],
        default='mwrr',
        help='APY calculation method (default: mwrr)'
    )
    portfolio_parser.add_argument(
        '--format',
        choices=['table', 'json'],
        default='table',
        help='Output format (default: table)'
    )
    portfolio_parser.add_argument(
        '--quiet', '-q',
        action='store_true',
        help='Suppress price data staleness warnings'
    )
    portfolio_parser.set_defaults(func=portfolio)
    
    # Export command
    export_parser = subparsers.add_parser('export', help='Export transactions to CSV')
    export_parser.add_argument(
        '--output',
        default='transactions_export.csv',
        help='Path to the output CSV file (default: transactions_export.csv)'
    )
    export_parser.add_argument(
        '--account',
        default='all',
        help='Account ID or display name to filter (default: all)'
    )
    export_parser.set_defaults(func=export_transactions)
    
    # Status command
    status_parser = subparsers.add_parser('status', help='Show system status')
    status_parser.set_defaults(func=status)
    
    # Reset command
    reset_parser = subparsers.add_parser('reset', help='Reset database state')
    reset_parser.add_argument(
        '--hard',
        action='store_true',
        help='Hard reset: delete all transactions, stats, and prices'
    )
    reset_parser.set_defaults(func=reset)
    
    args = parser.parse_args()
    
    if not args.command:
        parser.print_help()
        return 1
    
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())