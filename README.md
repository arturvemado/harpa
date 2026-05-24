<div align="center">
  <img 
    src="HARPA_logo.png" 
    alt="HARPA logo" 
    width="2000"
  />
</div>

# HARPA

**H**ub de **A**locação e **R**ecomendação de **P**ortfólios de **A**ções

## Setup

- [uv](https://docs.astral.sh/uv/) manages the environment and dependencies.

```bash
cd harpa
uv sync
```

Copy [`.env.example`](.env.example) to `.env`, set your key from the [Bolsai dashboard](https://usebolsai.com/), then export it in your shell (this project does not auto-load `.env` files):

```bash
export BOLSAI_API_KEY="sk_..."
```

## Export stock tickers (Bolsai)

The [Bolsai API](https://usebolsai.com/docs) lists tickers at `GET /api/v1/stocks` with `X-API-Key`. Many endpoints support `format=csv`; this exporter uses **JSON with pagination** so the `/stocks` response shape stays predictable.

The CSV writer keeps only tickers that match **four alphanumeric characters (no spaces) + a single trailing digit `3` or `4`** (e.g. `PETR4`, `VALE3`, `B3SA3`). `list_all_tickers()` still returns the full API list if you need it in code.

CLI (writes `tickers.csv` by default):

```bash
uv run harpa-export-tickers --output data/tickers.csv
```

Python:

```python
from pathlib import Path
from harpa.bolsai import BolsaiStocksClient

client = BolsaiStocksClient()
client.save_tickers_csv(Path("data/tickers.csv"))
```

Options: `--bdi-code` (default `02` = ações), `--include-bdi-column` for an extra `bdi_code` column in the CSV.

## API reference

- Documentation: [https://usebolsai.com/docs](https://usebolsai.com/docs)
- Base URL: `https://api.usebolsai.com/api/v1`
