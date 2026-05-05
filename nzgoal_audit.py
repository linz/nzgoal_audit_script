"""Audit NZGOAL form outcomes against LINZ Data Service items.

This script reads item IDs and expected publish outcomes from a TSV export,
fetches layers, tables, and datasets from the LDS API (optionally filtered by
date range), and prints grouped results for:
- publish
- publish with restrictions
- do not publish

It also reports items found on the LDS that are missing from the NZGOAL form.
"""

import argparse
import csv
import datetime as dt
import re
import sys

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

LAYERS = "https://data.linz.govt.nz/services/api/v1/layers/"
TABLES = "https://data.linz.govt.nz/services/api/v1/tables/"
DATASETS = "https://data.linz.govt.nz/services/api/v1/datasets/"  # Pointclouds are datasets, not layers


def _read_tsv(tsv_file: str) -> dict[str, str]:
    """Read a TSV file."""
    with open(tsv_file, "r", encoding="utf-8-sig", newline="") as tsv:
        reader = csv.reader(tsv, delimiter="\t")
        header = next(reader)

        # Check an id column was added to tsv as per instructions
        if header[0].upper() != "ID":
            print(
                'EXITING - The first column has not be titled "id" as per the instructions'
            )
            sys.exit()

        form_data = {}
        for row in reader:
            ids = re.sub(" +", "", row[0])

            # last question answered
            pos_last_q = [i for i, x in enumerate(row) if x != ""][-1]
            str_last_q = header[pos_last_q]

            # iterate over ids in id coloum
            for id in ids.split(","):
                # Categorise based on last answer
                if re.match(r".*RELEASE\.$", str_last_q):
                    form_data[id] = "pub"  # publish
                elif re.match(
                    r"(.*release to restricted audience.*|.*obtain the relevant rights.*)",
                    str_last_q,
                ):
                    form_data[id] = "pwr"  # publish with restrictions
                elif re.match(r"(^Do not publish.*)", str_last_q):
                    form_data[id] = "dnp"  # do not publish
                else:
                    print(
                        "SCRIPT FAILED WHEN MATCHING TSV HEADER TEXT WITH CODE FOR "
                        "CATEGORISATION. \n Has the forms outcomes wording changed?"
                    )

                    sys.exit()
    return form_data


def _next_url(link_header: str | None) -> str | None:
    """Return the URL for rel="page-next" """
    if not link_header:
        return None
    for part in link_header.split(","):
        part = part.strip()
        if 'rel="page-next"' not in part:
            continue
        url_segment = part.split(";", 1)[0].strip()
        if url_segment.startswith("<") and url_segment.endswith(">"):
            return url_segment[1:-1]
    return None


def _parse_iso_date(date_str: str) -> dt.date | None:
    """Parse ISO 8601 dates with or without microseconds."""
    if not date_str:
        return None
    try:
        # Try parsing with microseconds first
        return dt.datetime.strptime(date_str, "%Y-%m-%dT%H:%M:%S.%fZ").date()
    except ValueError:
        # Fall back to parsing without microseconds
        return dt.datetime.strptime(date_str, "%Y-%m-%dT%H:%M:%SZ").date()


def _in_date_range(
    value: dt.date | None, date_from: dt.date | None, date_to: dt.date | None
) -> bool:
    """Return True if value is within the optional inclusive range."""
    if date_from is None and date_to is None:
        return True
    if value is None:
        return False
    if date_from is not None and value < date_from:
        return False
    if date_to is not None and value > date_to:
        return False
    return True


def _session() -> requests.Session:
    """Return a requests Session with automatic retries on transient server errors."""
    session = requests.Session()
    retry = Retry(total=5, backoff_factor=1, status_forcelist=[502, 503, 504])
    session.mount("https://", HTTPAdapter(max_retries=retry))
    return session


def _iter_items(root_url: str) -> list[dict]:
    """Return all items across paginated API results."""
    session = _session()
    all_items: list[dict] = []
    url: str | None = root_url
    while url:
        response = session.get(url)
        response.raise_for_status()
        data = response.json()
        items = data.get("results", data) if isinstance(data, dict) else data
        all_items.extend(items)
        url = _next_url(response.headers.get("Link"))
    return all_items


def _fetch_all_layers_tables(
    root_url: str, date_from: dt.date | None = None, date_to: dt.date | None = None
) -> list[tuple[str, str, str]]:
    """Return a list of (id, title, published_at) from all pages.

    If date_from/date_to are provided, only include items whose published_at
    date lies within that inclusive range. If neither is provided, all
    items are returned.
    """
    results: list[tuple[str, str, str]] = []
    for item in _iter_items(root_url):
        item_id = item.get("id")
        title = item.get("title")
        published_at = _parse_iso_date(item.get("first_published_at"))
        if (
            item_id is None
            or title is None
            or not _in_date_range(published_at, date_from, date_to)
        ):
            continue
        results.append(
            (
                str(item_id),
                str(title),
                str(published_at) if published_at is not None else "",
            )
        )
    return results


def _fetch_all_datasets(
    root_url: str, date_from: dt.date | None = None, date_to: dt.date | None = None
) -> list[tuple[str, str, str]]:
    """Return a list of (id, title, created_at) from all pages.

    If date_from/date_to are provided, only include items whose created_at
    date lies within that inclusive range. If neither is provided, all
    items are returned.

    created_at field is retrieved from the dataset details endpoint, as it is not included in the list endpoint.
    """
    results: list[tuple[str, str, str]] = []
    for item in _iter_items(root_url):
        item_id = item.get("id")
        title = item.get("title")
        dataset_url = item.get("url")
        if item_id is None or title is None or not dataset_url:
            continue

        dataset_response = _session().get(str(dataset_url))
        dataset_response.raise_for_status()
        dataset_item = dataset_response.json()
        created_at = _parse_iso_date(dataset_item.get("created_at"))
        if not _in_date_range(created_at, date_from, date_to):
            continue

        results.append(
            (
                str(item_id),
                str(title),
                str(created_at) if created_at is not None else "",
            )
        )
    return results


def _print_category(
    title: str,
    code: str,
    all_items: list[tuple[str, str, str]],
    form_data: dict[str, str],
) -> None:
    print(f"=== {title} ===")
    for item_id, name, pub in all_items:
        if form_data.get(item_id.strip()) == code:
            print(f"ID {item_id}: {name} ({pub})")


def main():
    parser = argparse.ArgumentParser(description="Audit NZGOAL forms against LINZ data")
    parser.add_argument(
        "--from-date", "-fd", dest="date_from", help="Audit from date (dd/mm/yy)"
    )
    parser.add_argument(
        "--to-date", "-td", dest="date_to", help="Audit to date (dd/mm/yy)"
    )
    parser.add_argument(
        "--tsv-file", "-tsv", required=True, dest="tsv_file", help="TSV file path"
    )

    args = parser.parse_args()

    tsv_file = args.tsv_file
    print(f"Reading IDs from TSV: {tsv_file}")
    form_data = _read_tsv(tsv_file)

    date_from = (
        dt.datetime.strptime(args.date_from, "%d/%m/%y").date()
        if args.date_from
        else None
    )
    date_to = (
        dt.datetime.strptime(args.date_to, "%d/%m/%y").date() if args.date_to else None
    )

    print("Fetching all Layers...")
    layers = _fetch_all_layers_tables(LAYERS, date_from, date_to)
    print(f"Fetched {len(layers)} layers")

    print("Fetching all Tables...")
    tables = _fetch_all_layers_tables(TABLES, date_from, date_to)
    print(f"Fetched {len(tables)} tables")

    print("Fetching all Datasets...")
    datasets = _fetch_all_datasets(DATASETS, date_from, date_to)
    print(f"Fetched {len(datasets)} datasets")

    all_items = layers + tables + datasets

    _print_category(
        "Layers/tables/datasets Publish status", "pub", all_items, form_data
    )
    print()
    _print_category(
        "Layers/tables/datasets Publish With Restrictions", "pwr", all_items, form_data
    )
    print()
    _print_category(
        "Layers/tables/datasets Do Not Publish", "dnp", all_items, form_data
    )

    print()
    print("=== Data missing from NZGOAL Audit Form ===")
    missing_data = [
        (i, name, pub) for i, name, pub in all_items if str(i) not in form_data
    ]
    if not missing_data:
        print("(none)")
    else:
        for i, name, pub in missing_data:
            print(f"Id {i}: {name} ({pub})")


if __name__ == "__main__":
    sys.exit(main())
