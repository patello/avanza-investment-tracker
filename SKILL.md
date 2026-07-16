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

<!-- INSERT:SECTION:## CLI Reference -->

<!-- INSERT:SECTION:## Special Cases -->

## See Also

- **Detailed workflows**: [references/workflows.md](references/workflows.md)
- **Troubleshooting guide**: [references/troubleshooting.md](references/troubleshooting.md)
