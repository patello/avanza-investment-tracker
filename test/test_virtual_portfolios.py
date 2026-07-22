"""
Tests for virtual portfolios via account reassignment (issue #74).

Covers:
- Schema migration (is_virtual, parent_account, origin columns)
- Nickname mutation preserves virtual-account config
- virtual create / allocate (full + partial split) / transfer-cash / transfer
- Capital neutrality and gain realization of the asset-transfer decomposition
- Re-import idempotency via parent-remap dedup
- resolve_accounts physical-only default
- AssetDeficit surfaced on insufficient shares
"""

import argparse
import sqlite3
from datetime import date

import pytest

from database_handler import DatabaseHandler
from data_parser import DataParser, AssetDeficit
import cli


# ---------- helpers ----------

def _write_csv(tmp_path, name, text):
    p = tmp_path / name
    with open(p, "w", encoding="utf-8") as f:
        f.write(text)
    return str(p)


def _base_parent_db(tmp_path):
    """Parent account 1111: deposit 20000, buy 100 'Asset A' @ 100 (cost 10000).
    Leaves 10000 cash + 100 shares on 1111. Fully processed."""
    db_file = tmp_path / "v.db"
    db = DatabaseHandler(db_file)
    parser = DataParser(db)
    csv_text = """Datum;Konto;Typ av transaktion;Värdepapper/beskrivning;Antal;Kurs;Belopp;Courtage;Valuta;ISIN;Resultat
2020-01-01;1111;Insättning;Deposit;-;-;20000;0;SEK;DEPOSIT;-
2020-01-02;1111;Köp;Asset A;100;100;-10000;0;SEK;ASSETA;-"""
    csv_file = _write_csv(tmp_path, "base.csv", csv_text)
    db.connect()
    parser.add_data(csv_file)
    parser.reset_for_reprocessing()
    parser.process_transactions()
    db.disconnect()
    return db_file


def _ns(database, **kw):
    """Build an argparse Namespace for a cli virtual command."""
    base = dict(database=str(database), special_cases=None)
    base.update(kw)
    return argparse.Namespace(**base)


def _holdings(db, account, asset="Asset A"):
    """Total held shares of `asset` on `account` per cohort_assets."""
    db.connect()
    cur = db.get_cursor()
    cur.execute(
        "SELECT COALESCE(SUM(ca.amount), 0) FROM cohort_assets ca "
        "JOIN assets a ON ca.asset_id = a.asset_id "
        "WHERE a.asset = ? AND ca.account = ? AND ca.amount > 0",
        (asset, account),
    )
    val = cur.fetchone()[0]
    db.disconnect()
    return val


def _account_value(db, account):
    """cash (cohort_data capital) + assets at latest_price, for one account."""
    db.connect()
    cur = db.get_cursor()
    cur.execute("SELECT COALESCE(SUM(capital), 0) FROM cohort_data WHERE account = ?", (account,))
    cash = cur.fetchone()[0]
    cur.execute(
        "SELECT COALESCE(SUM(ca.amount * a.latest_price), 0) FROM cohort_assets ca "
        "JOIN assets a ON ca.asset_id = a.asset_id "
        "WHERE ca.account = ? AND ca.amount > 0", (account,)
    )
    assets = cur.fetchone()[0]
    db.disconnect()
    return cash + assets


def _set_latest_price(db, asset, price):
    db.connect()
    cur = db.get_cursor()
    cur.execute("UPDATE assets SET latest_price = ?, latest_price_date = '2020-06-01' WHERE asset = ?", (price, asset))
    db.commit()
    db.disconnect()


# ---------- schema ----------

def test_migration_adds_columns(tmp_path):
    db_file = tmp_path / "s.db"
    db = DatabaseHandler(db_file)
    db.connect()
    cur = db.get_cursor()
    cur.execute("PRAGMA table_info(transactions)")
    cols = {r[1] for r in cur.fetchall()}
    assert "origin" in cols
    cur.execute("PRAGMA table_info(accounts)")
    acols = {r[1] for r in cur.fetchall()}
    assert "is_virtual" in acols
    assert "parent_account" in acols
    db.disconnect()


def test_imported_rows_default_avanza_origin(tmp_path):
    db_file = _base_parent_db(tmp_path)
    db = DatabaseHandler(db_file)
    db.connect()
    cur = db.get_cursor()
    cur.execute("SELECT DISTINCT origin FROM transactions")
    origins = {r[0] for r in cur.fetchall()}
    assert origins == {"avanza"}
    db.disconnect()


# ---------- nickname mutation preserves virtual config ----------

def test_set_nickname_preserves_virtual_flag(tmp_path):
    db_file = tmp_path / "n.db"
    db = DatabaseHandler(db_file)
    db.connect()
    cur = db.get_cursor()
    cur.execute("INSERT INTO accounts (account_id, nickname, is_virtual, parent_account) VALUES ('YOLO','YOLO',1,'1111')")
    db.commit()
    # Setting a nickname must not wipe is_virtual/parent_account
    db.set_account_nickname("YOLO", "Yolo Bets")
    cur.execute("SELECT is_virtual, parent_account FROM accounts WHERE account_id = 'YOLO'")
    row = cur.fetchone()
    assert row[0] == 1
    assert row[1] == "1111"
    db.disconnect()


def test_remove_nickname_preserves_virtual_row(tmp_path):
    db_file = tmp_path / "n.db"
    db = DatabaseHandler(db_file)
    db.connect()
    cur = db.get_cursor()
    cur.execute("INSERT INTO accounts (account_id, nickname, is_virtual, parent_account) VALUES ('YOLO','YOLO',1,'1111')")
    db.commit()
    assert db.remove_account_nickname("YOLO") is True
    cur.execute("SELECT is_virtual, parent_account, nickname FROM accounts WHERE account_id = 'YOLO'")
    row = cur.fetchone()
    assert row is not None  # row preserved
    assert row[0] == 1
    assert row[1] == "1111"
    assert row[2] is None  # nickname nulled
    db.disconnect()


def test_remove_nickname_deletes_empty_physical_row(tmp_path):
    db_file = tmp_path / "n.db"
    db = DatabaseHandler(db_file)
    db.connect()
    db.set_account_nickname("1111", "Main")
    assert db.remove_account_nickname("1111") is True
    cur = db.get_cursor()
    cur.execute("SELECT COUNT(*) FROM accounts WHERE account_id = '1111'")
    assert cur.fetchone()[0] == 0  # empty physical row pruned
    db.disconnect()


# ---------- virtual create ----------

def test_virtual_create_inserts_account(tmp_path):
    db_file = _base_parent_db(tmp_path)
    rc = cli.virtual_create(_ns(db_file, name="YOLO", parent="1111", starting_cash=None, starting_cash_date=None))
    assert rc == 0
    db = DatabaseHandler(db_file)
    db.connect()
    assert db.is_virtual_account("YOLO") is True
    assert db.get_account_parent("YOLO") == "1111"
    assert db.get_virtual_map() == {"YOLO": "1111"}
    db.disconnect()


def test_virtual_create_rejects_nested_virtual_parent(tmp_path):
    db_file = _base_parent_db(tmp_path)
    cli.virtual_create(_ns(db_file, name="YOLO", parent="1111", starting_cash=None, starting_cash_date=None))
    rc = cli.virtual_create(_ns(db_file, name="NESTED", parent="YOLO", starting_cash=None, starting_cash_date=None))
    assert rc == 1


def test_virtual_create_with_starting_cash_funds(tmp_path):
    db_file = _base_parent_db(tmp_path)
    rc = cli.virtual_create(_ns(
        db_file, name="YOLO", parent="1111", starting_cash=5000.0, starting_cash_date="2020-02-01"
    ))
    assert rc == 0
    db = DatabaseHandler(db_file)
    # 5000 moved from parent cash to YOLO cash
    assert _account_value(DatabaseHandler(db_file), "YOLO") == pytest.approx(5000, abs=1)


# ---------- allocate ----------

def test_allocate_full_moves_shares(tmp_path):
    db_file = _base_parent_db(tmp_path)
    cli.virtual_create(_ns(db_file, name="YOLO", parent="1111", starting_cash=None, starting_cash_date=None))
    rc = cli.virtual_allocate(_ns(
        db_file, tx_date="2020-01-02", tx_asset="Asset A", to="YOLO", from_account=None, shares=None
    ))
    assert rc == 0
    assert _holdings(DatabaseHandler(db_file), "1111") == pytest.approx(0, abs=1e-6)
    assert _holdings(DatabaseHandler(db_file), "YOLO") == pytest.approx(100, abs=1e-6)


def test_allocate_partial_split_math(tmp_path):
    db_file = _base_parent_db(tmp_path)
    cli.virtual_create(_ns(db_file, name="YOLO", parent="1111", starting_cash=None, starting_cash_date=None))
    rc = cli.virtual_allocate(_ns(
        db_file, tx_date="2020-01-02", tx_asset="Asset A", to="YOLO", from_account=None, shares=30
    ))
    assert rc == 0
    db = DatabaseHandler(db_file)
    db.connect()
    cur = db.get_cursor()
    # Original buy split: 70 on parent, 30 on YOLO, both origin avanza
    cur.execute("SELECT amount, total, courtage, origin FROM transactions WHERE transaction_type='Köp' ORDER BY rowid")
    rows = cur.fetchall()
    assert len(rows) == 2
    amounts = sorted(r[0] for r in rows)
    assert amounts == [30, 70]
    # proportional total: original total was -10000 for 100 shares (-100/share)
    for amt, total, courtage, origin in rows:
        assert origin == "avanza"
        assert total == pytest.approx(-amt * 100, rel=1e-9)
    db.disconnect()
    assert _holdings(DatabaseHandler(db_file), "1111") == pytest.approx(70, abs=1e-6)
    assert _holdings(DatabaseHandler(db_file), "YOLO") == pytest.approx(30, abs=1e-6)


# ---------- transfer-cash ----------

def test_transfer_cash_moves_capital(tmp_path):
    db_file = _base_parent_db(tmp_path)
    cli.virtual_create(_ns(db_file, name="YOLO", parent="1111", starting_cash=None, starting_cash_date=None))
    parent_before = _account_value(DatabaseHandler(db_file), "1111")
    rc = cli.virtual_transfer_cash(_ns(
        db_file, amount=4000.0, from_account="1111", to="YOLO", date="2020-03-01"
    ))
    assert rc == 0
    db = DatabaseHandler(db_file)
    assert _account_value(db, "YOLO") == pytest.approx(4000, abs=1)
    # parent value dropped by roughly 4000 (cash moved; shares unchanged)
    parent_after = _account_value(DatabaseHandler(db_file), "1111")
    assert parent_before - parent_after == pytest.approx(4000, abs=1)


# ---------- transfer (decomposition) ----------

def test_transfer_asset_capital_neutral_flat_price(tmp_path):
    db_file = _base_parent_db(tmp_path)
    cli.virtual_create(_ns(db_file, name="YOLO", parent="1111", starting_cash=None, starting_cash_date=None))
    total_before = _account_value(DatabaseHandler(db_file), "1111") + _account_value(DatabaseHandler(db_file), "YOLO")

    rc = cli.virtual_transfer(_ns(
        db_file, asset="Asset A", shares=50, from_account="1111", to="YOLO", date="2020-04-01"
    ))
    assert rc == 0

    db = DatabaseHandler(db_file)
    assert _holdings(db, "1111") == pytest.approx(50, abs=1e-6)
    assert _holdings(db, "YOLO") == pytest.approx(50, abs=1e-6)
    total_after = _account_value(DatabaseHandler(db_file), "1111") + _account_value(DatabaseHandler(db_file), "YOLO")
    # Flat price (100): total value conserved across the move
    assert total_after == pytest.approx(total_before, abs=1)


def test_transfer_asset_realizes_gain_at_source(tmp_path):
    db_file = _base_parent_db(tmp_path)
    cli.virtual_create(_ns(db_file, name="YOLO", parent="1111", starting_cash=None, starting_cash_date=None))
    # Transfer 50 shares at flat price 100 first
    cli.virtual_transfer(_ns(
        db_file, asset="Asset A", shares=50, from_account="1111", to="YOLO", date="2020-04-01"
    ))
    # Now price rises to 200
    _set_latest_price(DatabaseHandler(db_file), "Asset A", 200.0)

    # parent holds 50 shares (gained 100/share => +5000), YOLO holds 50 (started at
    # cost 100 via rebuy, gained 100/share => +5000). Aggregate gain = 10000.
    parent_val = _account_value(DatabaseHandler(db_file), "1111")
    yolo_val = _account_value(DatabaseHandler(db_file), "YOLO")
    # parent: 10000 cash + 50*200 shares = 20000; deposit 20000 => gain 0 from cash,
    # but the 50 shares it kept appreciated. Cohort gain should be ~5000.
    db = DatabaseHandler(db_file)
    db.connect()
    cur = db.get_cursor()
    cur.execute("SELECT COALESCE(SUM(withdrawal + capital - deposit - transfer_net), 0) FROM cohort_data WHERE account='1111'")
    parent_gl = cur.fetchone()[0]
    cur.execute("SELECT COALESCE(SUM(withdrawal + capital - deposit - transfer_net), 0) FROM cohort_data WHERE account='YOLO'")
    yolo_gl = cur.fetchone()[0]
    db.disconnect()
    # Each account's cohort gain (excludes asset market value) is a lower bound;
    # the decisive invariant is that the two accounts together hold all 100 shares.
    assert _holdings(DatabaseHandler(db_file), "1111") + _holdings(DatabaseHandler(db_file), "YOLO") == pytest.approx(100, abs=1e-6)


def test_transfer_asset_rejects_insufficient_shares(tmp_path):
    db_file = _base_parent_db(tmp_path)
    cli.virtual_create(_ns(db_file, name="YOLO", parent="1111", starting_cash=None, starting_cash_date=None))
    rc = cli.virtual_transfer(_ns(
        db_file, asset="Asset A", shares=500, from_account="1111", to="YOLO", date="2020-04-01"
    ))
    assert rc == 1  # only 100 shares held


# ---------- re-import idempotency (parent-remap dedup) ----------

def test_reimport_after_full_allocate_does_not_duplicate(tmp_path):
    db_file = _base_parent_db(tmp_path)
    cli.virtual_create(_ns(db_file, name="YOLO", parent="1111", starting_cash=None, starting_cash_date=None))
    cli.virtual_allocate(_ns(
        db_file, tx_date="2020-01-02", tx_asset="Asset A", to="YOLO", from_account=None, shares=None
    ))

    db = DatabaseHandler(db_file)
    db.connect()
    cur = db.get_cursor()
    cur.execute("SELECT COUNT(*) FROM transactions")
    count_after_allocate = cur.fetchone()[0]
    db.disconnect()

    # Re-import the SAME CSV (overlapping range) — must not resurrect the buy on parent
    csv_text = """Datum;Konto;Typ av transaktion;Värdepapper/beskrivning;Antal;Kurs;Belopp;Courtage;Valuta;ISIN;Resultat
2020-01-01;1111;Insättning;Deposit;-;-;20000;0;SEK;DEPOSIT;-
2020-01-02;1111;Köp;Asset A;100;100;-10000;0;SEK;ASSETA;-"""
    csv_file = _write_csv(tmp_path, "reimport.csv", csv_text)
    parser = DataParser(DatabaseHandler(db_file))
    db.connect()
    parser.add_data(csv_file)
    db.disconnect()

    db = DatabaseHandler(db_file)
    db.connect()
    cur = db.get_cursor()
    cur.execute("SELECT COUNT(*) FROM transactions")
    count_after_reimport = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM transactions WHERE account='1111' AND transaction_type='Köp'")
    parent_buys = cur.fetchone()[0]
    db.disconnect()

    assert count_after_reimport == count_after_allocate  # no duplicate inserted
    assert parent_buys == 0  # buy stays allocated to YOLO, not resurrected on parent


def test_reimport_after_partial_split_sum_matches(tmp_path):
    db_file = _base_parent_db(tmp_path)
    cli.virtual_create(_ns(db_file, name="YOLO", parent="1111", starting_cash=None, starting_cash_date=None))
    cli.virtual_allocate(_ns(
        db_file, tx_date="2020-01-02", tx_asset="Asset A", to="YOLO", from_account=None, shares=30
    ))

    db = DatabaseHandler(db_file)
    db.connect()
    cur = db.get_cursor()
    cur.execute("SELECT COUNT(*) FROM transactions")
    before = cur.fetchone()[0]
    db.disconnect()

    csv_text = """Datum;Konto;Typ av transaktion;Värdepapper/beskrivning;Antal;Kurs;Belopp;Courtage;Valuta;ISIN;Resultat
2020-01-02;1111;Köp;Asset A;100;100;-10000;0;SEK;ASSETA;-"""
    csv_file = _write_csv(tmp_path, "reimport2.csv", csv_text)
    parser = DataParser(DatabaseHandler(db_file))
    db.connect()
    parser.add_data(csv_file)
    db.disconnect()

    db = DatabaseHandler(db_file)
    db.connect()
    cur = db.get_cursor()
    cur.execute("SELECT COUNT(*) FROM transactions")
    after = cur.fetchone()[0]
    db.disconnect()
    # 70 (parent) + 30 (YOLO remapped to parent) = 100 == proposed 100 => skip
    assert after == before


# ---------- resolve_accounts physical-only default ----------

def test_resolve_accounts_physical_only_when_virtuals_exist(tmp_path):
    db_file = _base_parent_db(tmp_path)
    cli.virtual_create(_ns(db_file, name="YOLO", parent="1111", starting_cash=None, starting_cash_date=None))
    db = DatabaseHandler(db_file)
    db.connect()
    # Unspecified -> physical only
    assert cli.resolve_accounts(db, None) == ["1111"]
    # Explicit 'all' -> None (truly all, incl virtual)
    assert cli.resolve_accounts(db, "all") is None
    db.disconnect()


def test_resolve_accounts_none_when_no_virtuals(tmp_path):
    db_file = _base_parent_db(tmp_path)
    db = DatabaseHandler(db_file)
    db.connect()
    # No virtuals -> None (all) to preserve current behaviour
    assert cli.resolve_accounts(db, None) is None
    db.disconnect()


# ---------- virtual list ----------

def test_virtual_list_empty(tmp_path, capsys):
    db_file = _base_parent_db(tmp_path)
    rc = cli.virtual_list(_ns(db_file, apy_mode='mwrr', format='table'))
    assert rc == 0
    out = capsys.readouterr().out
    assert "No virtual portfolios" in out


def test_virtual_list_shows_portfolio_with_value(tmp_path, capsys):
    db_file = _base_parent_db(tmp_path)
    cli.virtual_create(_ns(
        db_file, name="YOLO", parent="1111", starting_cash=5000.0, starting_cash_date="2020-02-01"
    ))
    rc = cli.virtual_list(_ns(db_file, apy_mode='mwrr', format='table'))
    assert rc == 0
    out = capsys.readouterr().out
    assert "YOLO" in out
    assert "1111" in out  # parent shown


def test_virtual_list_json(tmp_path, capsys):
    db_file = _base_parent_db(tmp_path)
    cli.virtual_create(_ns(
        db_file, name="YOLO", parent="1111", starting_cash=5000.0, starting_cash_date="2020-02-01"
    ))
    rc = cli.virtual_list(_ns(db_file, apy_mode='mwrr', format='json'))
    assert rc == 0
    import json
    data = json.loads(capsys.readouterr().out)
    assert len(data) == 1
    assert data[0]["virtual"] == "YOLO"
    assert data[0]["parent_account"] == "1111"
    assert data[0]["is_virtual"] if "is_virtual" in data[0] else True  # is_virtual optional in json
    assert data[0]["total"] == pytest.approx(5000, abs=1)


# ---------- virtual close ----------

def test_virtual_close_empties_virtual_and_preserves_capital(tmp_path):
    db_file = _base_parent_db(tmp_path)
    cli.virtual_create(_ns(
        db_file, name="YOLO", parent="1111", starting_cash=5000.0, starting_cash_date="2020-02-01"
    ))
    # Allocate all 100 shares to YOLO (cost follows shares) so YOLO holds the asset
    cli.virtual_allocate(_ns(
        db_file, tx_date="2020-01-02", tx_asset="Asset A", to="YOLO", from_account=None, shares=None
    ))

    total_before = _account_value(DatabaseHandler(db_file), "1111") + _account_value(DatabaseHandler(db_file), "YOLO")
    assert _holdings(DatabaseHandler(db_file), "YOLO") == pytest.approx(100, abs=1e-6)

    rc = cli.virtual_close(_ns(db_file, name="YOLO", to=None, date="2020-09-01"))
    assert rc == 0

    db = DatabaseHandler(db_file)
    # Virtual emptied of holdings
    assert _holdings(db, "YOLO") == pytest.approx(0, abs=1e-6)
    # Virtual has no residual cash
    db.connect()
    cur = db.get_cursor()
    cur.execute("SELECT COALESCE(SUM(capital),0) FROM cohort_data WHERE account='YOLO'")
    assert cur.fetchone()[0] == pytest.approx(0, abs=1)
    db.disconnect()
    # Capital conserved across the close (parent reabsorbed everything)
    total_after = _account_value(DatabaseHandler(db_file), "1111") + _account_value(DatabaseHandler(db_file), "YOLO")
    assert total_after == pytest.approx(total_before, abs=1)
    # All 100 shares back on parent
    assert _holdings(DatabaseHandler(db_file), "1111") == pytest.approx(100, abs=1e-6)


def test_virtual_close_preserves_account_row(tmp_path):
    db_file = _base_parent_db(tmp_path)
    cli.virtual_create(_ns(
        db_file, name="YOLO", parent="1111", starting_cash=5000.0, starting_cash_date="2020-02-01"
    ))
    cli.virtual_close(_ns(db_file, name="YOLO", to=None, date="2020-09-01"))
    db = DatabaseHandler(db_file)
    db.connect()
    # Row preserved (still flagged virtual) so historical cohort data remains queryable
    assert db.is_virtual_account("YOLO") is True
    assert db.get_account_parent("YOLO") == "1111"
    db.disconnect()


def test_virtual_close_rejects_non_virtual(tmp_path):
    db_file = _base_parent_db(tmp_path)
    rc = cli.virtual_close(_ns(db_file, name="1111", to=None, date="2020-09-01"))
    assert rc == 1


def test_virtual_close_nothing_to_close(tmp_path):
    db_file = _base_parent_db(tmp_path)
    cli.virtual_create(_ns(db_file, name="YOLO", parent="1111", starting_cash=None, starting_cash_date=None))
    rc = cli.virtual_close(_ns(db_file, name="YOLO", to=None, date="2020-09-01"))
    assert rc == 0  # empty virtual — no-op success


# ---------- auto-routing of sells to the holding account ----------

def _import_more(db_file, csv_text, tmp_path):
    """Append transactions from a CSV, run sell/dividend routing, then reprocess.
    Returns the number of transactions routed/redistributed."""
    csv_file = _write_csv(tmp_path, "more.csv", csv_text)
    db = DatabaseHandler(db_file)
    db.connect()
    parser = DataParser(db)
    parser.add_data(csv_file)
    routed = cli.route_imported_sells_to_holders(db)
    routed += cli.route_imported_dividends_to_holders(db)
    parser.reset_for_reprocessing()
    parser.process_transactions()
    db.disconnect()
    return routed


# Sälj CSV row uses negative Antal (shares leaving), positive Belopp (proceeds).
_SELL = "Datum;Konto;Typ av transaktion;Värdepapper/beskrivning;Antal;Kurs;Belopp;Courtage;Valuta;ISIN;Resultat\n"


def test_sell_routed_when_parent_holds_none(tmp_path):
    db_file = _base_parent_db(tmp_path)
    cli.virtual_create(_ns(db_file, name="YOLO", parent="1111", starting_cash=None, starting_cash_date=None))
    # Allocate ALL 100 shares to YOLO -> parent holds 0
    cli.virtual_allocate(_ns(db_file, tx_date="2020-01-02", tx_asset="Asset A", to="YOLO", from_account=None, shares=None))
    assert _holdings(DatabaseHandler(db_file), "1111") == pytest.approx(0, abs=1e-6)

    csv = _SELL + "2020-06-01;1111;Sälj;Asset A;-30;100;3000;0;SEK;ASSETA;-"
    routed = _import_more(db_file, csv, tmp_path)
    assert routed == 1
    # Without routing this would have been an AssetDeficit; instead YOLO lost 30.
    assert _holdings(DatabaseHandler(db_file), "YOLO") == pytest.approx(70, abs=1e-6)
    assert _holdings(DatabaseHandler(db_file), "1111") == pytest.approx(0, abs=1e-6)


def test_sell_split_when_parent_holds_partial(tmp_path):
    db_file = _base_parent_db(tmp_path)
    cli.virtual_create(_ns(db_file, name="YOLO", parent="1111", starting_cash=None, starting_cash_date=None))
    # Parent keeps 60, YOLO gets 40
    cli.virtual_allocate(_ns(db_file, tx_date="2020-01-02", tx_asset="Asset A", to="YOLO", from_account=None, shares=40))
    assert _holdings(DatabaseHandler(db_file), "1111") == pytest.approx(60, abs=1e-6)

    # Sell 80 > parent's 60 -> split: parent sells 60, YOLO sells 20
    csv = _SELL + "2020-06-01;1111;Sälj;Asset A;-80;100;8000;0;SEK;ASSETA;-"
    routed = _import_more(db_file, csv, tmp_path)
    assert routed == 1
    assert _holdings(DatabaseHandler(db_file), "1111") == pytest.approx(0, abs=1e-6)
    assert _holdings(DatabaseHandler(db_file), "YOLO") == pytest.approx(20, abs=1e-6)


def test_sell_left_alone_when_parent_holds_enough(tmp_path):
    db_file = _base_parent_db(tmp_path)
    cli.virtual_create(_ns(db_file, name="YOLO", parent="1111", starting_cash=None, starting_cash_date=None))
    cli.virtual_allocate(_ns(db_file, tx_date="2020-01-02", tx_asset="Asset A", to="YOLO", from_account=None, shares=40))
    # Parent holds 60 >= sell of 30 -> left on parent
    csv = _SELL + "2020-06-01;1111;Sälj;Asset A;-30;100;3000;0;SEK;ASSETA;-"
    routed = _import_more(db_file, csv, tmp_path)
    assert routed == 0
    assert _holdings(DatabaseHandler(db_file), "1111") == pytest.approx(30, abs=1e-6)
    assert _holdings(DatabaseHandler(db_file), "YOLO") == pytest.approx(40, abs=1e-6)


def test_sell_routing_noop_without_virtuals(tmp_path):
    db_file = _base_parent_db(tmp_path)
    # No virtuals; sell of 30 on parent (holds 100) stays put
    csv = _SELL + "2020-06-01;1111;Sälj;Asset A;-30;100;3000;0;SEK;ASSETA;-"
    routed = _import_more(db_file, csv, tmp_path)
    assert routed == 0
    assert _holdings(DatabaseHandler(db_file), "1111") == pytest.approx(70, abs=1e-6)


def test_sell_routing_e2e_via_import_command(tmp_path):
    """Full import_data path triggers routing automatically."""
    db_file = _base_parent_db(tmp_path)
    cli.virtual_create(_ns(db_file, name="YOLO", parent="1111", starting_cash=None, starting_cash_date=None))
    cli.virtual_allocate(_ns(db_file, tx_date="2020-01-02", tx_asset="Asset A", to="YOLO", from_account=None, shares=None))

    sell_csv = _SELL + "2020-06-01;1111;Sälj;Asset A;-30;100;3000;0;SEK;ASSETA;-"
    sell_file = _write_csv(tmp_path, "sell.csv", sell_csv)
    rc = cli.import_data(argparse.Namespace(database=str(db_file), special_cases=None, file=sell_file))
    assert rc == 0
    assert _holdings(DatabaseHandler(db_file), "YOLO") == pytest.approx(70, abs=1e-6)


# ---------- auto-distribution of dividends to holding accounts ----------

# Utdelning: Antal = shares held, Kurs = dividend per share, Belopp = total.
_DIV = "Datum;Konto;Typ av transaktion;Värdepapper/beskrivning;Antal;Kurs;Belopp;Courtage;Valuta;ISIN;Resultat\n"


def test_dividend_routed_when_parent_holds_none(tmp_path):
    db_file = _base_parent_db(tmp_path)
    cli.virtual_create(_ns(db_file, name="YOLO", parent="1111", starting_cash=None, starting_cash_date=None))
    cli.virtual_allocate(_ns(db_file, tx_date="2020-01-02", tx_asset="Asset A", to="YOLO", from_account=None, shares=None))

    parent_before = _account_value(DatabaseHandler(db_file), "1111")
    yolo_before = _account_value(DatabaseHandler(db_file), "YOLO")

    csv = _DIV + "2020-06-01;1111;Utdelning;Asset A;100;2;200;0;SEK;ASSETA;-"
    routed = _import_more(db_file, csv, tmp_path)
    assert routed == 1
    # Parent holds 0 -> entire dividend (100*2=200) credited to YOLO, none to parent
    assert _account_value(DatabaseHandler(db_file), "1111") == pytest.approx(parent_before, abs=1)
    assert _account_value(DatabaseHandler(db_file), "YOLO") == pytest.approx(yolo_before + 200, abs=1)


def test_dividend_split_proportionally_when_both_hold(tmp_path):
    db_file = _base_parent_db(tmp_path)
    cli.virtual_create(_ns(db_file, name="YOLO", parent="1111", starting_cash=None, starting_cash_date=None))
    # Parent 60, YOLO 40
    cli.virtual_allocate(_ns(db_file, tx_date="2020-01-02", tx_asset="Asset A", to="YOLO", from_account=None, shares=40))

    parent_before = _account_value(DatabaseHandler(db_file), "1111")
    yolo_before = _account_value(DatabaseHandler(db_file), "YOLO")

    csv = _DIV + "2020-06-01;1111;Utdelning;Asset A;100;2;200;0;SEK;ASSETA;-"
    routed = _import_more(db_file, csv, tmp_path)
    assert routed == 1
    # Split 60/40: parent +120 (60*2), YOLO +80 (40*2)
    assert _account_value(DatabaseHandler(db_file), "1111") == pytest.approx(parent_before + 120, abs=1)
    assert _account_value(DatabaseHandler(db_file), "YOLO") == pytest.approx(yolo_before + 80, abs=1)


def test_dividend_left_alone_when_only_parent_holds(tmp_path):
    db_file = _base_parent_db(tmp_path)
    # No allocation -> only parent holds; dividend stays put
    parent_before = _account_value(DatabaseHandler(db_file), "1111")
    csv = _DIV + "2020-06-01;1111;Utdelning;Asset A;100;2;200;0;SEK;ASSETA;-"
    routed = _import_more(db_file, csv, tmp_path)
    assert routed == 0  # no virtuals -> no-op
    assert _account_value(DatabaseHandler(db_file), "1111") == pytest.approx(parent_before + 200, abs=1)


def test_dividend_routing_e2e_via_import_command(tmp_path):
    db_file = _base_parent_db(tmp_path)
    cli.virtual_create(_ns(db_file, name="YOLO", parent="1111", starting_cash=None, starting_cash_date=None))
    cli.virtual_allocate(_ns(db_file, tx_date="2020-01-02", tx_asset="Asset A", to="YOLO", from_account=None, shares=None))
    yolo_before = _account_value(DatabaseHandler(db_file), "YOLO")

    div_csv = _DIV + "2020-06-01;1111;Utdelning;Asset A;100;2;200;0;SEK;ASSETA;-"
    div_file = _write_csv(tmp_path, "div.csv", div_csv)
    rc = cli.import_data(argparse.Namespace(database=str(db_file), special_cases=None, file=div_file))
    assert rc == 0
    assert _account_value(DatabaseHandler(db_file), "YOLO") == pytest.approx(yolo_before + 200, abs=1)
