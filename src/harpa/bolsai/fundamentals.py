"""Fetch historical fundamentals via the Bolsai API and export to CSV.

Historical fundamentals (`GET /fundamentals/{ticker}/history`) are documented as a
**Pro** feature. The free tier has a low daily request cap (200/day); exporting
every ticker in ``data/tickers.csv`` requires Pro and/or splitting the work across
days. Rate limits reset at midnight UTC; ``429`` responses raise
:class:`~harpa.bolsai.exceptions.BolsaiRateLimitError`.
"""

from __future__ import annotations

import argparse
import csv
import logging
import os
from pathlib import Path
from typing import Any

import httpx

from harpa.bolsai.exceptions import (
    BolsaiAuthError,
    BolsaiHTTPError,
    BolsaiRateLimitError,
)
from harpa.bolsai.tickers import DEFAULT_BASE_URL

logger = logging.getLogger(__name__)


def _csv_cell(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


class BolsaiFundamentalsClient:
    """HTTP client for Bolsai ``/fundamentals/{ticker}/history``."""

    def __init__(
        self,
        api_key: str | None = None,
        *,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 30.0,
    ) -> None:
        key = api_key if api_key is not None else os.environ.get("BOLSAI_API_KEY")
        if not key or not key.strip():
            raise ValueError(
                "Missing API key: pass api_key=... or set the BOLSAI_API_KEY environment variable."
            )
        self._api_key = key.strip()
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout

    def _raise_for_status(self, response: httpx.Response) -> None:
        if response.status_code == 401:
            raise BolsaiAuthError("Bolsai API returned 401 Unauthorized. Check your X-API-Key.")
        if response.status_code == 429:
            raise BolsaiRateLimitError(
                "Bolsai API returned 429 Too Many Requests (daily quota). "
                "Limits reset at midnight UTC; check X-RateLimit-Remaining on successful responses."
            )
        if not response.is_success:
            raise BolsaiHTTPError(
                f"Bolsai API error {response.status_code}: {response.text[:500]}",
                status_code=response.status_code,
            )

    def fetch_history(
        self,
        ticker: str,
        *,
        limit: int = 80,
        client: httpx.Client | None = None,
    ) -> dict[str, Any]:
        """Return parsed JSON from ``GET /fundamentals/{ticker}/history``.

        ``limit`` is the number of quarters (1–80 per API docs).

        If ``client`` is omitted, a short-lived client is used. For many tickers,
        pass a shared :class:`httpx.Client` from the caller.
        """
        close_after = False
        if client is None:
            client = httpx.Client(timeout=self._timeout, follow_redirects=True)
            close_after = True
        try:
            url = f"{self._base_url}/fundamentals/{ticker}/history"
            headers = {"X-API-Key": self._api_key}
            params = {"limit": limit}
            response = client.get(url, headers=headers, params=params)
            self._raise_for_status(response)
            return response.json()
        finally:
            if close_after:
                client.close()

    @staticmethod
    def read_tickers_csv(path: str | Path) -> list[str]:
        """Read tickers from a CSV with a ``ticker`` column (one symbol per row)."""
        p = Path(path)
        tickers: list[str] = []
        with p.open(newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            if reader.fieldnames is None or "ticker" not in reader.fieldnames:
                raise ValueError(f"CSV {p} must have a header row including a 'ticker' column.")
            for row in reader:
                t = (row.get("ticker") or "").strip()
                if t:
                    tickers.append(t)
        return tickers

    def save_historical_fundamentals_csv(
        self,
        input_path: str | Path,
        output_path: str | Path,
        *,
        limit: int = 80,
    ) -> Path:
        """Fetch fundamentals history for each ticker in ``input_path`` and write ``output_path``.

        Each row is one quarter: ``ticker``, ``corporate_name``, then every key
        present in that quarter's ``history`` object. Column union is built across
        all rows; missing values are empty. Tickers that return 403/404, HTTP
        5xx (server errors, including persistent ``internal_server_error`` for
        some symbols), or empty ``history`` are skipped; each ticker is logged
        as success (✅) or failure (❌) with details. :class:`BolsaiRateLimitError`
        is re-raised so callers can retry later.
        """
        if not 1 <= limit <= 80:
            raise ValueError("limit must be between 1 and 80 (Bolsai API).")

        tickers = self.read_tickers_csv(input_path)
        rows: list[dict[str, Any]] = []
        history_key_union: set[str] = set()

        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)

        with httpx.Client(timeout=self._timeout, follow_redirects=True) as client:
            for ticker in tickers:
                try:
                    data = self.fetch_history(ticker, limit=limit, client=client)
                except BolsaiRateLimitError:
                    raise
                except BolsaiHTTPError as e:
                    if e.status_code in (403, 404) or 500 <= e.status_code < 600:
                        logger.info("❌ %s: %s", ticker, e)
                        continue
                    logger.info("❌ %s: %s", ticker, e)
                    raise

                history = data.get("history") or []
                if not history:
                    logger.info("❌ %s: empty history", ticker)
                    continue

                corporate_name = str(data.get("corporate_name") or "")
                resp_ticker = str(data.get("ticker") or ticker)

                n_quarters = 0
                for item in history:
                    if not isinstance(item, dict):
                        continue
                    history_key_union.update(item.keys())
                    row = {k: _csv_cell(v) for k, v in item.items()}
                    row["ticker"] = resp_ticker
                    row["corporate_name"] = corporate_name
                    rows.append(row)
                    n_quarters += 1

                if n_quarters == 0:
                    logger.info(
                        "❌ %s: history had no dict-shaped quarter entries",
                        ticker,
                    )
                else:
                    logger.info("✅ %s (%d quarter row(s))", ticker, n_quarters)

        extra_keys = sorted(history_key_union - {"ticker", "corporate_name"})
        fieldnames = ["ticker", "corporate_name"] + extra_keys

        with out.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(
                f,
                fieldnames=fieldnames,
                restval="",
                extrasaction="ignore",
            )
            writer.writeheader()
            for row in rows:
                writer.writerow({k: row.get(k, "") for k in fieldnames})

        return out


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Export Bolsai historical fundamentals (/fundamentals/{ticker}/history) "
            "for tickers listed in a CSV to a single flat CSV. Requires API access "
            "appropriate for the historical fundamentals endpoint (typically Pro)."
        )
    )
    parser.add_argument(
        "-i",
        "--input",
        default="data/tickers.csv",
        help="Input CSV path with a 'ticker' column (default: data/tickers.csv).",
    )
    parser.add_argument(
        "-o",
        "--output",
        default="data/historical_fundamentals.csv",
        help="Output CSV path (default: data/historical_fundamentals.csv).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=80,
        help="Number of quarterly history points per ticker (1-80, default: 80).",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    client = BolsaiFundamentalsClient()
    path = client.save_historical_fundamentals_csv(
        args.input,
        args.output,
        limit=args.limit,
    )
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        next(reader, None)
        n = sum(1 for _ in reader)
    print(f"Wrote {path.resolve()} ({n} rows).")


if __name__ == "__main__":
    main()
