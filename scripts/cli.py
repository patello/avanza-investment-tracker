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


def prices_are_fresh(db, max_age_days=1):
    """
    Check if prices are fresh (updated within max_age_days).
    
    Returns:
    bool: True if prices are fresh, False otherwise
    str: Oldest price date or None if no prices
    """
    cur = db.get_cursor()
    # Get oldest price date for assets with amount > 0
    result = cur.execute(
        "SELECT MIN(latest_price_date) FROM assets WHERE amount > 0"
    ).fetchone()
    
    if not result or not result[0]:
        return False, None  # No prices or no assets
    
    oldest_price_date = datetime.strptime(result[0], '%Y-%m-%d').date()
    today = datetime.today().date()
    
    is_fresh = oldest_price_date >= today - timedelta(days=max_age_days)
    return is_fresh, oldest_price_date

def any_assets_need_prices(db):
    """
    Check if any assets with amount > 0 have no price date.
    
    Returns:
    bool: True if any assets need prices, False otherwise
    """
    cur = db.get_cursor()
    result = cur.execute(
        "SELECT COUNT(*) FROM assets WHERE amount > 0 AND latest_price_date IS NULL"
    ).fetchone()
    
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
    if args.update_prices == 'always':
        # Force price update
        fresh, oldest_date = prices_are_fresh(db)
        if fresh:
            logging.info(f"Prices are already fresh (oldest: {oldest_date}), updating anyway...")
        try:
            stat_calc = StatCalculator(db)
            stat_calc.update_prices(force=True)
            
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
        fresh, oldest_date = prices_are_fresh(db)
        need_prices = any_assets_need_prices(db)
        
        if not fresh or need_prices:
            if need_prices:
                logging.info("Some assets have no prices, updating...")
            else:
                logging.info(f"Prices are stale (oldest: {oldest_date}), updating...")
            
            try:
                stat_calc = StatCalculator(db)
                stat_calc.update_prices(force=True)
                
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
    apy_mode = getattr(args, 'apy_mode', 'modified-dietz')
    # Force recalculation if APY mode changed
    last_apy_mode = db.get_metadata('last_apy_mode') or 'modified-dietz'
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
        data_parser.reset_processed_transactions()
        
        # Clear metadata
        for key in ['last_processed', 'last_stats_calculation']:
            db.set_metadata(key, '')
        
        logging.info("Database reset successfully")
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
    if args.update_prices == 'always':
        # Force price update
        fresh, oldest_date = prices_are_fresh(db)
        if fresh:
            logging.info(f"Prices are already fresh (oldest: {oldest_date}), updating anyway...")
        try:
            stat_calc = StatCalculator(db)
            stat_calc.update_prices(force=True)
            
            # Update metadata
            now = datetime.now().isoformat()
            db.set_metadata('last_price_update', now)
            
            logging.info("Prices updated successfully")
        except Exception as e:
            logging.error(f"Failed to update prices: {e}")
            return 1
            
    elif args.update_prices == 'auto':
        fresh, oldest_date = prices_are_fresh(db)
        need_prices = any_assets_need_prices(db)
        
        if not fresh or need_prices:
            if need_prices:
                logging.info("Some assets have no prices, updating...")
            else:
                logging.info(f"Prices are stale (oldest: {oldest_date}), updating...")
            
            try:
                stat_calc = StatCalculator(db)
                stat_calc.update_prices(force=True)
                
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
        stat_calc.print_account_summary(accounts=accounts)
        return 0
        
    except Exception as e:
        logging.error(f"Failed to show account summary: {e}")
        return 1


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
        choices=['modified-dietz', 'twrr'],
        default='modified-dietz',
        help='APY calculation method (default: modified-dietz)'
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
    accounts_parser.set_defaults(func=accounts_summary)
    
    # Status command
    status_parser = subparsers.add_parser('status', help='Show system status')
    status_parser.set_defaults(func=status)
    
    # Reset command
    reset_parser = subparsers.add_parser('reset', help='Reset database state')
    reset_parser.set_defaults(func=reset)
    
    args = parser.parse_args()
    
    if not args.command:
        parser.print_help()
        return 1
    
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())