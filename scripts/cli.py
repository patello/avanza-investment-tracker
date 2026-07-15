#!/usr/bin/env python3
"""
CLI interface for the Avanza investment tracker.

Provides command-line access to data import, transaction processing,
price updates, and statistics calculation.
"""

import argparse
import sys
import logging
from datetime import datetime, timedelta

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


def stats(args):
    """Smart statistics command with automatic updates."""
    db = get_db(args)
    
    # Parse account filter
    account_arg = args.account.strip()
    accounts = None
    
    if account_arg.lower() == 'all':
        accounts = None  # None means all accounts
    elif account_arg.lower() == 'default':
        # Get default accounts from metadata
        default_accounts_str = db.get_metadata('default_accounts')
        if default_accounts_str is None or default_accounts_str == '':
            accounts = None  # No default set, use all accounts
        else:
            accounts = [acc.strip() for acc in default_accounts_str.split(',')]
    else:
        # Comma-separated list of accounts
        accounts = [acc.strip() for acc in account_arg.split(',')]
    
    # Parse as_of, start_date, end_date
    as_of = None
    start_date = None
    end_date = None
    
    try:
        as_of = parse_date_bound(getattr(args, 'as_of', None), is_start_bound=False)
        start_date = parse_date_bound(getattr(args, 'start_date', None), is_start_bound=True)
        end_date = parse_date_bound(getattr(args, 'end_date', None), is_start_bound=False)
        
        # If both are specified, start_date and end_date/as_of work together
        if as_of is not None and end_date is None:
            end_date = as_of
    except ValueError as e:
        logging.error(str(e))
        return 1

    has_date_filters = (as_of is not None or start_date is not None or end_date is not None)

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
    temp_db = None
    temp_db_path = None
    try:
        if has_date_filters:
            import shutil
            import os
            import time
            
            # Determine target_date: the end bound of calculations
            target_date = end_date if end_date is not None else as_of
            if target_date is None:
                target_date = datetime.today().date()
                
            temp_db_path = f"{db.db_file}_temp_as_of_{int(time.time())}.db"
            shutil.copy2(db.db_file, temp_db_path)
            
            temp_db = DatabaseHandler(temp_db_path)
            temp_db.connect()
            
            # Delete transactions after target_date
            temp_cur = temp_db.get_cursor()
            temp_cur.execute("DELETE FROM transactions WHERE date > ?", (target_date.isoformat(),))
            
            # Reset tables
            temp_db.reset_table("cohort_data")
            temp_db.reset_table("cohort_assets")
            temp_db.reset_table("cohort_cash_flows")
            temp_db.reset_table("assets")
            temp_cur.execute("UPDATE transactions SET processed = 0")
            temp_db.commit()
            
            # Run parser
            special_cases = SpecialCases(args.special_cases) if getattr(args, 'special_cases', None) else None
            parser = DataParser(temp_db, special_cases)
            parser.process_transactions()
            
            # Resolve prices as of target_date
            assets = temp_cur.execute("SELECT asset_id FROM assets").fetchall()
            for (asset_id,) in assets:
                price_row = temp_cur.execute("""
                    SELECT price, price_date FROM asset_prices
                    WHERE asset_id = ? AND price_date <= ?
                    ORDER BY price_date DESC LIMIT 1
                """, (asset_id, target_date.isoformat())).fetchone()
                if price_row:
                    temp_cur.execute("""
                        UPDATE assets
                        SET latest_price = ?, latest_price_date = ?
                        WHERE asset_id = ?
                    """, (price_row[0], price_row[1], asset_id))
            temp_db.commit()
            
            # Calculate stats dynamically
            stat_calc = StatCalculator(temp_db)
            stat_calc.calculate_cohort_stats(apy_mode=apy_mode, today=target_date)
            stat_calc.calculate_year_stats(apy_mode=apy_mode, today=target_date)
            
            active_db = temp_db
        else:
            active_db = db
            
        stat_calc = StatCalculator(active_db)
        period = args.period
        if period == 'default':
            period = active_db.get_metadata('default_stats_period') or 'month'

        kwargs = {
            'period': period,
            'deposits': args.deposits,
            'apy_mode': apy_mode,
            'start_date': start_date,
            'end_date': end_date
        }
        
        # Add account filter if specified
        if accounts is not None:
            kwargs['accounts'] = accounts
        
        if getattr(args, 'format', 'table') == 'json':
            import json
            def serialize_date(d):
                if hasattr(d, 'isoformat'):
                    return d.isoformat()
                return str(d)
                
            if args.accumulated:
                acc_stats = stat_calc.get_accumulated(**kwargs)
                json_data = []
                for (date_val, acc_net_deposit, acc_value, acc_gainloss) in acc_stats:
                    json_data.append({
                        'date': serialize_date(date_val),
                        'deposit': acc_net_deposit,
                        'value': acc_value,
                        'gain_loss': acc_gainloss
                    })
                print(json.dumps(json_data, indent=2))
            else:
                stats_list = stat_calc.get_stats(**kwargs)
                json_data = []
                for (date_val, deposit, withdrawal, value, total_gainloss, realized_gainloss, unrealized_gainloss, total_gainloss_per, realized_gainloss_per, unrealized_gainloss_per, annual_per_yield) in stats_list:
                    if deposit > 0:
                        json_data.append({
                            'date': serialize_date(date_val),
                            'deposit': deposit,
                            'withdrawal': withdrawal,
                            'value': value,
                            'total_gainloss': total_gainloss,
                            'total_gainloss_percent': total_gainloss_per,
                            'realized_gainloss': realized_gainloss,
                            'realized_gainloss_percent': realized_gainloss_per,
                            'unrealized_gainloss': unrealized_gainloss,
                            'unrealized_gainloss_percent': unrealized_gainloss_per,
                            'apy': annual_per_yield
                        })
                print(json.dumps(json_data, indent=2))
        else:
            if args.accumulated:
                stat_calc.print_accumulated(**kwargs)
            else:
                stat_calc.print_stats(**kwargs)
        return 0
        
    except Exception as e:
        logging.error(f"Failed to show statistics: {e}")
        return 1
    finally:
        if temp_db is not None:
            temp_db.disconnect()
            import gc
            gc.collect()
            if temp_db_path and os.path.exists(temp_db_path):
                try:
                    os.remove(temp_db_path)
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
    account_arg = args.account.strip()
    accounts = None
    
    if account_arg.lower() == 'all':
        accounts = None  # None means all accounts
    elif account_arg.lower() == 'default':
        # Get default accounts from metadata
        default_accounts_str = db.get_metadata('default_accounts')
        if default_accounts_str is None or default_accounts_str == '':
            accounts = None  # No default set, use all accounts
        else:
            accounts = [acc.strip() for acc in default_accounts_str.split(',')]
    else:
        # Comma-separated list of accounts
        accounts = [acc.strip() for acc in account_arg.split(',')]
    
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


def portfolio(args):
    """Show portfolio snapshot or change over a period."""
    db = get_db(args)
    cur = db.get_cursor()
    
    # Parse dates
    as_of = None
    start_date = None
    end_date = None
    
    try:
        as_of = parse_date_bound(getattr(args, 'as_of', None), is_start_bound=False)
        start_date = parse_date_bound(getattr(args, 'start_date', None), is_start_bound=True)
        end_date = parse_date_bound(getattr(args, 'end_date', None), is_start_bound=False)
        
        if as_of is not None and end_date is None:
            end_date = as_of
    except ValueError as e:
        logging.error(str(e))
        return 1
            
    # Parse account filter
    account_arg = args.account.strip()
    accounts = None
    if account_arg.lower() == 'all':
        accounts = None
    elif account_arg.lower() == 'default':
        default_accounts_str = db.get_metadata('default_accounts')
        if default_accounts_str:
            accounts = [acc.strip() for acc in default_accounts_str.split(',')]
    else:
        accounts = [acc.strip() for acc in account_arg.split(',')]
        
    # Get all distinct accounts from transactions up to end_date (or all time if end_date is None)
    if accounts is None:
        query = "SELECT DISTINCT account FROM transactions"
        params = []
        if end_date:
            query += " WHERE date <= ?"
            params.append(end_date)
        query += " ORDER BY account"
        accounts = [row[0] for row in cur.execute(query, params).fetchall()]
        
    nicknames = db.get_all_account_nicknames()
    
    def get_display_name(account):
        return nicknames.get(account, account)
        
    def get_snapshot(as_of_date):
        summaries = {}
        for account in accounts:
            # 1. Cash balance
            cash_query = "SELECT SUM(total) FROM transactions WHERE account = ?"
            cash_params = [account]
            if as_of_date:
                cash_query += " AND date <= ?"
                cash_params.append(as_of_date)
                
            cur.execute(cash_query, cash_params)
            cash_row = cur.fetchone()
            cash = cash_row[0] if cash_row and cash_row[0] is not None else 0.0
            
            # 2. Asset holdings
            holdings_query = """
                SELECT asset_name, SUM(amount)
                FROM transactions
                WHERE account = ? AND transaction_type IN ('Köp', 'Sälj', 'Tillgångsinsättning', 'Värdepappersuttag', 'Byte')
            """
            holdings_params = [account]
            if as_of_date:
                holdings_query += " AND date <= ?"
                holdings_params.append(as_of_date)
            holdings_query += " GROUP BY asset_name HAVING ABS(SUM(amount)) > 0.0001"
            
            cur.execute(holdings_query, holdings_params)
            holdings = cur.fetchall()
            
            assets_value = 0.0
            for asset_name, amt in holdings:
                if as_of_date:
                    price_query = """
                        SELECT ap.price FROM asset_prices ap
                        JOIN assets a ON ap.asset_id = a.asset_id
                        WHERE a.asset = ? AND ap.price_date <= ?
                        ORDER BY ap.price_date DESC LIMIT 1
                    """
                    cur.execute(price_query, (asset_name, as_of_date))
                    p_row = cur.fetchone()
                    if p_row:
                        price = p_row[0]
                    else:
                        cur.execute("SELECT latest_price FROM assets WHERE asset = ?", (asset_name,))
                        fallback_row = cur.fetchone()
                        price = fallback_row[0] if fallback_row and fallback_row[0] is not None else 0.0
                else:
                    cur.execute("SELECT latest_price FROM assets WHERE asset = ?", (asset_name,))
                    p_row = cur.fetchone()
                    price = p_row[0] if p_row and p_row[0] is not None else 0.0
                    
                assets_value += amt * price
                
            summaries[account] = {
                'cash': cash,
                'assets': assets_value,
                'total': cash + assets_value
            }
        return summaries

    if start_date is not None:
        # Comparison mode: show change from start_date to end_date (pure market return)
        start_vals = get_snapshot(start_date)
        end_vals = get_snapshot(end_date)
        
        comparison = []
        for account in accounts:
            s_val = start_vals[account]['total']
            e_val = end_vals[account]['total']
            
            # Query net deposits using external capital flows SQL view
            cur.execute("""
                SELECT COALESCE(SUM(flow_amount), 0) FROM v_external_capital_flows
                WHERE account = ? AND date > ? AND date <= ?
            """, (account, start_date.isoformat() if hasattr(start_date, 'isoformat') else start_date, end_date.isoformat() if hasattr(end_date, 'isoformat') else end_date))
            net_dep = cur.fetchone()[0]
            ret_sek = e_val - s_val - net_dep
            ret_pct = (ret_sek / s_val * 100) if s_val > 0 else 0.0
            total_change = e_val - s_val
            
            comparison.append({
                'account': account,
                'display_name': get_display_name(account),
                'start_value': s_val,
                'end_value': e_val,
                'net_deposits': net_dep,
                'return_sek': ret_sek,
                'return_percent': ret_pct,
                'total_change': total_change
            })
            
        if args.format == 'json':
            import json
            print(json.dumps(comparison, indent=2))
        else:
            if not comparison:
                print("No portfolio data found")
                return 0
                
            total_start = sum(c['start_value'] for c in comparison)
            total_end = sum(c['end_value'] for c in comparison)
            total_net_deposits = sum(c['net_deposits'] for c in comparison)
            total_return_sek = total_end - total_start - total_net_deposits
            total_return_pct = (total_return_sek / total_start * 100) if total_start > 0 else 0.0
            total_change_sek = total_end - total_start
            
            name_width = max(max(len(c['display_name']) for c in comparison), 7)
            header = f"{'Account':<{name_width}} {'Start Value':>12} {'End Value':>12} {'Net Deposits':>14} {'Return':>14} {'Return (%)':>12} {'Total Change':>14}"
            print(header)
            print("-" * len(header))
            
            for c in comparison:
                dep_sign = "+" if c['net_deposits'] > 0 else ""
                ret_sign = "+" if c['return_sek'] > 0 else ""
                pct_sign = "+" if c['return_percent'] > 0 else ""
                chg_sign = "+" if c['total_change'] > 0 else ""
                
                dep_str = f"{dep_sign}{c['net_deposits']:,.0f}" if c['net_deposits'] != 0 else "0"
                ret_str = f"{ret_sign}{c['return_sek']:,.0f}"
                pct_str = f"{pct_sign}{c['return_percent']:.1f}%"
                chg_str = f"{chg_sign}{c['total_change']:,.0f}"
                
                print(f"{c['display_name']:<{name_width}} {c['start_value']:>12.0f} {c['end_value']:>12.0f} {dep_str:>14} {ret_str:>14} {pct_str:>12} {chg_str:>14}")
                
            print("-" * len(header))
            total_dep_sign = "+" if total_net_deposits > 0 else ""
            total_ret_sign = "+" if total_return_sek > 0 else ""
            total_pct_sign = "+" if total_return_pct > 0 else ""
            total_chg_sign = "+" if total_change_sek > 0 else ""
            
            total_dep_str = f"{total_dep_sign}{total_net_deposits:,.0f}" if total_net_deposits != 0 else "0"
            total_ret_str = f"{total_ret_sign}{total_return_sek:,.0f}"
            total_pct_str = f"{total_pct_sign}{total_return_pct:.1f}%"
            total_chg_str = f"{total_chg_sign}{total_change_sek:,.0f}"
            
    else:
        # Single snapshot mode
        vals = get_snapshot(end_date)
        
        # Calculate APY mode
        apy_mode = getattr(args, 'apy_mode', 'mwrr')
        mode_label = 'MWRR' if apy_mode == 'mwrr' else 'TWRR'
        
        # If we have a single account, show the detailed summary layout
        if len(accounts) == 1:
            account = accounts[0]
            nickname = nicknames.get(account, account)
            
            # Fetch cash flows totals
            cur.execute("""
                SELECT 
                    COALESCE(SUM(CASE WHEN flow_amount > 0 THEN flow_amount ELSE 0 END), 0) AS deposits,
                    COALESCE(SUM(CASE WHEN flow_amount < 0 THEN flow_amount ELSE 0 END), 0) AS withdrawals
                FROM v_external_capital_flows
                WHERE account = ?
            """, (account,))
            cf_row = cur.fetchone()
            deposits = cf_row[0] if cf_row else 0.0
            withdrawals = cf_row[1] if cf_row else 0.0
            net_invested = deposits + withdrawals # withdrawals is negative
            
            # Current value (cash + assets)
            cash = vals[account]['cash']
            assets = vals[account]['assets']
            current_value = cash + assets
            
            # Fetch gain/loss from account_cohort_stats
            cur.execute("""
                SELECT acc_total_gainloss FROM account_cohort_stats
                WHERE account = ?
                ORDER BY month DESC LIMIT 1
            """, (account,))
            gl_row = cur.fetchone()
            total_gainloss = gl_row[0] if gl_row and gl_row[0] is not None else (current_value - net_invested)
            
            gainloss_pct = (total_gainloss / deposits * 100) if deposits > 0 else 0.0
            
            # Calculate APY
            stat_calc = StatCalculator(db)
            apy, total_days = stat_calc.calculate_account_apy(account, apy_mode=apy_mode, end_date=end_date, current_value=current_value)
            
            if args.format == 'json':
                import json
                result = {
                    'account': account,
                    'display_name': nickname,
                    'deposits': deposits,
                    'withdrawals': abs(withdrawals),
                    'net_invested': net_invested,
                    'current_value': current_value,
                    'total_gain': total_gainloss,
                    'total_gain_percent': gainloss_pct,
                    'apy': apy,
                    'apy_mode': apy_mode
                }
                print(json.dumps(result, indent=2))
            else:
                # Print human-readable summary
                print(f"Account {account} ({nickname})")
                print(f"Deposits: {deposits:,.0f} SEK")
                print(f"Withdrawals: {abs(withdrawals):,.0f} SEK")
                print(f"Net invested: {net_invested:,.0f} SEK")
                print(f"Current value: {current_value:,.0f} SEK")
                
                # Sign formatting
                gl_sign = "+" if total_gainloss > 0 else ""
                gl_pct_sign = "+" if gainloss_pct > 0 else ""
                print(f"Total gain: {gl_sign}{total_gainloss:,.0f} SEK ({gl_pct_sign}{gainloss_pct:.1f}%)")
                if apy is not None:
                    print(f"APY: {apy:.1f}% ({mode_label})")
                else:
                    print("APY: N/A")
                    
        else:
            # Multi-account table view
            summaries = []
            stat_calc = StatCalculator(db)
            
            for account in accounts:
                nickname = nicknames.get(account, account)
                cash = vals[account]['cash']
                assets = vals[account]['assets']
                current_value = cash + assets
                
                # Fetch deposits/withdrawals
                cur.execute("""
                    SELECT 
                        COALESCE(SUM(CASE WHEN flow_amount > 0 THEN flow_amount ELSE 0 END), 0) AS deposits,
                        COALESCE(SUM(CASE WHEN flow_amount < 0 THEN flow_amount ELSE 0 END), 0) AS withdrawals
                    FROM v_external_capital_flows
                    WHERE account = ?
                """, (account,))
                cf_row = cur.fetchone()
                deposits = cf_row[0] if cf_row else 0.0
                withdrawals = cf_row[1] if cf_row else 0.0
                net_invested = deposits + withdrawals
                
                cur.execute("""
                    SELECT acc_total_gainloss FROM account_cohort_stats
                    WHERE account = ?
                    ORDER BY month DESC LIMIT 1
                """, (account,))
                gl_row = cur.fetchone()
                total_gainloss = gl_row[0] if gl_row and gl_row[0] is not None else (current_value - net_invested)
                
                gainloss_pct = (total_gainloss / deposits * 100) if deposits > 0 else 0.0
                apy, total_days = stat_calc.calculate_account_apy(account, apy_mode=apy_mode, end_date=end_date, current_value=current_value)
                
                summaries.append({
                    'account': account,
                    'display_name': nickname,
                    'cash': cash,
                    'assets': assets,
                    'total': current_value,
                    'deposits': deposits,
                    'withdrawals': abs(withdrawals),
                    'net_invested': net_invested,
                    'total_gain': total_gainloss,
                    'total_gain_percent': gainloss_pct,
                    'apy': apy
                })
                
            if args.format == 'json':
                import json
                print(json.dumps(summaries, indent=2))
            else:
                if not summaries:
                    print("No portfolio data found")
                    return 0
                    
                total_cash = sum(s['cash'] for s in summaries)
                total_assets = sum(s['assets'] for s in summaries)
                total_total = sum(s['total'] for s in summaries)
                total_deposits = sum(s['deposits'] for s in summaries)
                total_withdrawals = sum(s['withdrawals'] for s in summaries)
                
                # Fetch combined total gain/loss from DB
                total_gainloss = 0.0
                for s in summaries:
                    total_gainloss += s['total_gain']
                    
                total_gain_percent = (total_gainloss / total_deposits * 100) if total_deposits > 0 else 0.0
                
                # Calculate combined APY
                combined_apy, total_days = stat_calc.calculate_account_apy(accounts, apy_mode=apy_mode, end_date=end_date, current_value=total_total)
                
                name_width = max(max(len(s['display_name']) for s in summaries), 7)
                
                header = f"{'Account':<{name_width}} {'Cash (SEK)':>12} {'Assets (SEK)':>12} {'Total (SEK)':>12} {'Deposits':>12} {'Gain/Loss':>14} {'Gain (%)':>10} {'APY':>8}"
                print(header)
                print("-" * len(header))
                
                for s in summaries:
                    gl_sign = "+" if s['total_gain'] > 0 else ""
                    gl_pct_sign = "+" if s['total_gain_percent'] > 0 else ""
                    apy_str = f"{s['apy']:.1f}%" if s['apy'] is not None else "N/A"
                    
                    print(f"{s['display_name']:<{name_width}} {s['cash']:>12.0f} {s['assets']:>12.0f} {s['total']:>12.0f} {s['deposits']:>12.0f} {gl_sign}{s['total_gain']:>13.0f} {gl_pct_sign}{s['total_gain_percent']:>8.1f}% {apy_str:>8}")
                    
                print("-" * len(header))
                
                total_gl_sign = "+" if total_gainloss > 0 else ""
                total_gl_pct_sign = "+" if total_gain_percent > 0 else ""
                total_apy_str = f"{combined_apy:.1f}%" if combined_apy is not None else "N/A"
                
                print(f"{'TOTAL':<{name_width}} {total_cash:>12.0f} {total_assets:>12.0f} {total_total:>12.0f} {total_deposits:>12.0f} {total_gl_sign}{total_gainloss:>13.0f} {total_gl_pct_sign}{total_gain_percent:>8.1f}% {total_apy_str:>8}")
            
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
        '--start-date',
        default=None,
        help='Start date for filtering statistics (formats: YYYY, YYYY-MM, YYYY-MM-DD)'
    )
    stats_parser.add_argument(
        '--end-date',
        default=None,
        help='End date for filtering statistics (formats: YYYY, YYYY-MM, YYYY-MM-DD)'
    )
    stats_parser.add_argument(
        '--format',
        choices=['table', 'json'],
        default='table',
        help='Output format (default: table)'
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
        '--start-date',
        default=None,
        help='Start date for period comparison (YYYY, YYYY-MM, YYYY-MM-DD)'
    )
    portfolio_parser.add_argument(
        '--end-date',
        default=None,
        help='End date for period comparison (YYYY, YYYY-MM, YYYY-MM-DD)'
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
    portfolio_parser.set_defaults(func=portfolio)
    
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