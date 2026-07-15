---
name: avanza-investment-tracker
description: "Process Avanza CSV exports, calculate TWRR/Modified Dietz returns, and track portfolio performance. Use when importing stock transactions, calculating investment returns, or managing portfolio data."
---

# Avanza Investment Tracker

Parse transaction CSVs and compute portfolio performance metrics.

## Quick Start

Run commands from your workspace root, specifying the paths to your database and CSV:

```bash
# 1. Import new transactions
python path/to/cli.py --database data/asset_data.db import path/to/transactions.csv

# 2. Update price cache and show statistics
python path/to/cli.py --database data/asset_data.db stats --update-prices auto

# 3. View portfolio allocation and APY
python path/to/cli.py --database data/asset_data.db portfolio --account default
```

## Data Storage Pattern

**User data lives OUTSIDE the skill directory.** Recommended structure:

```
workspace-finance/
├── skills/avanza-investment-tracker/   # Portable skill logic
│   ├── SKILL.md
│   ├── scripts/
│   └── assets/
└── data/avanza/                        # Private portfolio data
    ├── transactions.csv
    ├── special_cases.json
    └── asset_data.db
```

## CLI Reference

| Command | Description |
| :--- | :--- |
| `python scripts/cli.py import FILE` | Import transaction entries from Avanza CSV |
| `python scripts/cli.py stats [OPTIONS]` | Calculate and display performance statistics (TWRR, deposits) |
| `python scripts/cli.py accounts [OPTIONS]` | Display summary of all accounts with asset values and cash |
| `python scripts/cli.py portfolio [OPTIONS]` | Show portfolio holdings, market value, allocation %, and APY (MWRR/TWRR) |
| `python scripts/cli.py status` | Display system status (transaction counts, price dates, date range) |
| `python scripts/cli.py settings SUBCOMMAND` | Configure defaults and account nicknames |
| `python scripts/cli.py reset [--hard]` | Reset database state (`--hard` deletes data; default only marks unprocessed) |

### Global Options
- `--database PATH` (default: `data/asset_data.db`)
- `--special-cases PATH` (default: `data/special_cases.json`)

### Calculation & Output Options
- `--account ACCOUNTS`: Limit to specific accounts (e.g. `12345,67890`, `default`, or `all`)
- `--update-prices {auto,always,never}`: Controls when to fetch latest stock/fund prices from Avanza API
- `--update-all`: Update prices for all assets in the database, held or not
- `--as-of DATE`: View snapshot/stats as of a historical date (`YYYY-MM-DD`)
- `--start-date DATE --end-date DATE`: Calculate returns over a specific date range
- `--apy-mode {mwrr,twrr}`: APY calculation method (`mwrr` uses Modified Dietz; `twrr` uses Time-Weighted)
- `--format {table,json}`: Output formatting (default: `table`)

### Settings Subcommands
- `default-accounts ACCOUNTS`: Set default accounts (comma-separated list of IDs, or `all`)
- `default-stats-period {month,year}`: Set default period for performance reports
- `account-nickname [ACCOUNT] [NICKNAME]`: Set or list nicknames (`--list` to show all, `--remove ACCOUNT` to delete)

---

## Special Cases

Corporate actions (splits, spin-offs, zero-priced deposits) can be overridden by copying the template and defining rules:
```bash
cp assets/special_cases_template.json ../data/avanza/special_cases.json
```

## See Also

- **Detailed workflows**: [references/workflows.md](references/workflows.md)
- **Troubleshooting guide**: [references/troubleshooting.md](references/troubleshooting.md)
