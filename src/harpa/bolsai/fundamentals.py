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
import tempfile
from pathlib import Path
from typing import Any, Iterable

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


def _fieldnames_from_key_union(history_key_union: set[str]) -> list[str]:
    extra_keys = sorted(history_key_union - {"ticker", "corporate_name"})
    return ["ticker", "corporate_name"] + extra_keys


def _key_union_from_rows(rows: Iterable[dict[str, Any]]) -> set[str]:
    keys: set[str] = set()
    for row in rows:
        keys.update(row.keys())
    return keys


def _write_historical_fundamentals_csv_atomic(
    output_path: Path,
    fieldnames: list[str],
    rows: list[dict[str, Any]],
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        suffix=".csv",
        prefix=".historical_fundamentals_tmp_",
        dir=output_path.parent,
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(
                f,
                fieldnames=fieldnames,
                restval="",
                extrasaction="ignore",
            )
            writer.writeheader()
            for row in rows:
                writer.writerow({k: row.get(k, "") for k in fieldnames})
        os.replace(tmp_path, output_path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def _load_existing_historical_fundamentals_csv(
    path: Path,
) -> tuple[list[dict[str, Any]], set[tuple[str, str]]]:
    """Load existing fundamentals CSV rows and (ticker, reference_date) keys for dedup."""
    if not path.is_file():
        return [], set()
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None:
            return [], set()
        if "ticker" not in reader.fieldnames:
            raise ValueError(f"CSV {path} must have a header row including a 'ticker' column.")
        for row in reader:
            rows.append({k: (v if v is not None else "") for k, v in row.items()})
            t = (row.get("ticker") or "").strip()
            rd = (row.get("reference_date") or "").strip()
            if not rd:
                logger.warning(
                    "Existing row in %s without reference_date (ticker=%s); not used for deduplication.",
                    path,
                    t or "?",
                )
                continue
            seen.add((t, rd))
    return rows, seen


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

        fieldnames = _fieldnames_from_key_union(history_key_union)
        _write_historical_fundamentals_csv_atomic(out, fieldnames, rows)

        return out

    def update_historical_fundamentals_csv(
        self,
        input_path: str | Path,
        output_path: str | Path,
        *,
        limit: int = 80,
    ) -> Path:
        """Merge new quarter rows into ``output_path`` without re-fetching duplicates.

        Loads existing ``output_path`` if present. For each ticker in ``input_path``,
        fetches history and appends rows whose ``(ticker, reference_date)`` is not
        already in the file. Existing rows are unchanged. Column union is recomputed
        across old and new rows. If ``output_path`` does not exist, behaves like
        :meth:`save_historical_fundamentals_csv`. Writes via a temp file and
        ``os.replace`` for atomicity. :class:`BolsaiRateLimitError` is re-raised.
        """
        if not 1 <= limit <= 80:
            raise ValueError("limit must be between 1 and 80 (Bolsai API).")

        out = Path(output_path)
        existing_rows, seen = _load_existing_historical_fundamentals_csv(out)
        tickers = self.read_tickers_csv(input_path)
        appended: list[dict[str, Any]] = []

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

                n_read = 0
                n_new = 0
                n_missing_ref = 0
                for item in history:
                    if not isinstance(item, dict):
                        continue
                    n_read += 1
                    rd_raw = item.get("reference_date")
                    rd = "" if rd_raw is None else str(rd_raw).strip()
                    if not rd:
                        n_missing_ref += 1
                        logger.warning(
                            "%s: quarter entry without reference_date, skipped",
                            ticker,
                        )
                        continue
                    key = (resp_ticker.strip(), rd)
                    if key in seen:
                        continue
                    seen.add(key)
                    row = {k: _csv_cell(v) for k, v in item.items()}
                    row["ticker"] = resp_ticker
                    row["corporate_name"] = corporate_name
                    appended.append(row)
                    n_new += 1

                if n_read == 0:
                    logger.info(
                        "❌ %s: history had no dict-shaped quarter entries",
                        ticker,
                    )
                elif n_missing_ref == n_read:
                    logger.info(
                        "❌ %s: all quarter entries lack reference_date",
                        ticker,
                    )
                elif n_new == 0:
                    logger.info("✅ %s (up to date, 0 new quarter row(s))", ticker)
                else:
                    logger.info(
                        "✅ %s (+%d new quarter row(s), %d quarter dict(s) from API)",
                        ticker,
                        n_new,
                        n_read,
                    )

        all_rows = existing_rows + appended
        history_key_union = _key_union_from_rows(all_rows)
        fieldnames = _fieldnames_from_key_union(history_key_union)
        _write_historical_fundamentals_csv_atomic(out, fieldnames, all_rows)

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
    parser.add_argument(
        "--update",
        action="store_true",
        help=(
            "Merge into --output: keep existing rows and append only new "
            "(ticker, reference_date) pairs from the API. If the output file "
            "does not exist, behaves like a full export."
        ),
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    client = BolsaiFundamentalsClient()
    if args.update:
        path = client.update_historical_fundamentals_csv(
            args.input,
            args.output,
            limit=args.limit,
        )
        action = "Updated"
    else:
        path = client.save_historical_fundamentals_csv(
            args.input,
            args.output,
            limit=args.limit,
        )
        action = "Wrote"
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        next(reader, None)
        n = sum(1 for _ in reader)
    print(f"{action} {path.resolve()} ({n} rows).")


if __name__ == "__main__":
    main()
