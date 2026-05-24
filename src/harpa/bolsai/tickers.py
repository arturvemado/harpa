"""List B3 stock tickers via the Bolsai API and export to CSV."""

from __future__ import annotations

import argparse
import csv
import os
import re
from pathlib import Path
from typing import Any

import httpx

from harpa.bolsai.exceptions import (
    BolsaiAuthError,
    BolsaiHTTPError,
    BolsaiRateLimitError,
)

DEFAULT_BASE_URL = "https://api.usebolsai.com/api/v1"
DEFAULT_PAGE_LIMIT = 5000

# Four alphanumeric characters (no spaces) + listing digit 3 or 4 — e.g. PETR4, VALE3, B3SA3.
# For matching Brazilian stock tickers.
_TICKER_FOUR_ALNUM_THEN_3_OR_4 = re.compile(r"^[A-Za-z0-9]{4}[34]$")


def _ticker_four_alnum_suffix_3_or_4(ticker: str) -> bool:
    return bool(_TICKER_FOUR_ALNUM_THEN_3_OR_4.fullmatch(ticker))


class BolsaiStocksClient:
    """HTTP client for Bolsai `/stocks` listing (paginated tickers by BDI code)."""

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

    def _fetch_page(
        self,
        client: httpx.Client,
        *,
        bdi_code: str,
        limit: int,
        offset: int,
    ) -> dict[str, Any]:
        url = f"{self._base_url}/stocks"
        headers = {"X-API-Key": self._api_key}
        params = {"bdi_code": bdi_code, "limit": limit, "offset": offset}
        response = client.get(url, headers=headers, params=params)
        self._raise_for_status(response)
        return response.json()

    def list_all_tickers(self, *, bdi_code: str = "02") -> list[str]:
        """Return every ticker for ``bdi_code``, following API pagination (JSON, not format=csv)."""
        seen: set[str] = set()
        ordered: list[str] = []
        offset = 0
        total: int | None = None

        # Bolsai may respond with 307 (e.g. /stocks -> /stocks/); httpx defaults follow_redirects=False.
        with httpx.Client(timeout=self._timeout, follow_redirects=True) as client:
            while True:
                payload = self._fetch_page(
                    client,
                    bdi_code=bdi_code,
                    limit=DEFAULT_PAGE_LIMIT,
                    offset=offset,
                )
                if total is None and "total" in payload:
                    total = int(payload["total"])
                batch = payload.get("tickers") or []
                for t in batch:
                    if t not in seen:
                        seen.add(t)
                        ordered.append(t)
                if not batch:
                    break
                if total is not None and total > 0 and len(ordered) >= total:
                    break
                offset += len(batch)

        return sorted(ordered)

    def save_tickers_csv(
        self,
        path: str | Path,
        *,
        bdi_code: str = "02",
        include_bdi_column: bool = False,
    ) -> Path:
        """Write tickers to ``path`` as CSV (default: single ``ticker`` column).

        Only tickers matching **four alphanumeric characters (no spaces) + a trailing
        digit 3 or 4** (e.g. ``PETR4``, ``VALE3``, ``B3SA3``) are written; all others from
        the API are dropped.
        """
        out = Path(path)
        tickers = [
            t
            for t in self.list_all_tickers(bdi_code=bdi_code)
            if _ticker_four_alnum_suffix_3_or_4(t)
        ]
        out.parent.mkdir(parents=True, exist_ok=True)

        with out.open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            if include_bdi_column:
                writer.writerow(["ticker", "bdi_code"])
                for t in tickers:
                    writer.writerow([t, bdi_code])
            else:
                writer.writerow(["ticker"])
                for t in tickers:
                    writer.writerow([t])

        return out


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Export Bolsai-listed stock tickers (BDI 02 by default) to CSV, "
            "keeping only symbols that are four alphanumeric characters plus 3 or 4 "
            "(e.g. PETR4, VALE3, B3SA3; no spaces)."
        )
    )
    parser.add_argument(
        "-o",
        "--output",
        default="tickers.csv",
        help="Output CSV path (default: tickers.csv).",
    )
    parser.add_argument(
        "--bdi-code",
        default="02",
        help='BDI filter sent to /stocks (default: "02" = ações).',
    )
    parser.add_argument(
        "--include-bdi-column",
        action="store_true",
        help="Add a bdi_code column alongside ticker.",
    )
    args = parser.parse_args()

    client = BolsaiStocksClient()
    path = client.save_tickers_csv(
        args.output,
        bdi_code=args.bdi_code,
        include_bdi_column=args.include_bdi_column,
    )
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        next(reader, None)  # header
        n = sum(1 for _ in reader)
    print(f"Wrote {path.resolve()} ({n} tickers).")


if __name__ == "__main__":
    main()
