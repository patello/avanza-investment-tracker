# Grafana dashboard for virtual portfolios

This directory contains an importable Grafana dashboard that visualizes physical
accounts together with their virtual sub-portfolios. It queries the SQLite
database directly through the [frser-sqlite-datasource](https://grafana.com/grafana/plugins/frser-sqlite-datasource/)
plugin, using the virtual-portfolio-aware SQL views.

## Setup

1. **Install the SQLite datasource plugin** (if not already installed):
   ```bash
   grafana-cli plugins install frser-sqlite-datasource
   ```
2. **Add a datasource** of type *SQLite*, pointing at your tracker database
   (default `data/asset_data.db`). Name it `SQLite` (the dashboard's
   `DS_SQLITE` variable defaults to that name; change it in the dashboard
   settings if you use a different one).
3. **Import the dashboard**: Grafana → Dashboards → New → Import → upload
   `virtual_portfolios_dashboard.json` (or paste its contents). Select your
   SQLite datasource when prompted.

## Panels

| Panel | View / query | What it shows |
| :--- | :--- | :--- |
| Total value (incl. virtuals) | `v_virtual_portfolio_rollup` | `SUM(combined_total)` — physical own + all virtuals, no double counting |
| Physical own value | `v_virtual_portfolio_rollup` | `SUM(own_total)` |
| Virtual total value | `v_virtual_portfolio_rollup` | `SUM(virtual_total)` |
| Virtual portfolios | `accounts` | count of `is_virtual = 1` |
| Accounts (physical incl. virtual children) | `v_virtual_portfolio_rollup` | one row per physical account with own / virtual / combined totals |
| Per-account valuations | `v_account_current_valuations` | every account tagged `[V]`, with parent / cash / assets / total |
| Holdings by family | `v_account_asset_holdings` | shares grouped by parent family (parent + its virtuals) |
| Recent capital flows | `v_external_capital_flows` | latest money movements; `origin` distinguishes real (`avanza`) from internal virtual transfers (`virtual`) |

## Notes

- The dashboard refreshes hourly by default; the tracker only changes when you
  `import` new data or fetch prices, so a slow refresh is fine.
- APY / time-weighted performance is intentionally **not** in the dashboard —
  those calculations live in the Python engine. Use `cli.py report` for the
  performance comparison (virtual vs parent vs benchmark) and point a Grafana
  text panel at its output if you want it on screen.
- The SQL in every panel is plain SELECTs against the views documented in the
  top-level README's "SQL views" section, so you can copy them into any other
  BI tool.
