import argparse
import csv
import datetime as dt
import re
import sys

import requests

LAYERS = "https://data.linz.govt.nz/services/api/v1/layers/"
TABLES = "https://data.linz.govt.nz/services/api/v1/tables/"
DATASETS = "https://data.linz.govt.nz/services/api/v1/datasets/"  # Pointclouds are datasets, not layers


def read_tsv(tsv_file: str) -> dict[str, str]:
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


def fetch_all(
    root_url: str, date_from: dt.date | None = None, date_to: dt.date | None = None
) -> list[tuple[int, str, str]]:
    """Return a list of (id, title, published_at) from all pages.

    If date_from/date_to are provided, only include items whose published_at
    date lies within that inclusive range. If neither is provided, all
    items are returned.
    """
    results: list[tuple[int, str, str]] = []
    url: str | None = root_url
    while url:
        response = requests.get(url)
        response.raise_for_status()
        data = response.json()
        items = data.get("results", data) if isinstance(data, dict) else data
        for item in items:
            item_id = item.get("id")
            title = item.get("title")
            published_at = (
                dt.datetime.strptime(
                    item.get("first_published_at"), "%Y-%m-%dT%H:%M:%S.%fZ"
                ).date()
                if item.get("published_at")
                else None
            )
            if item_id is None or title is None:
                continue
            # Apply date filter if requested
            if date_from is not None or date_to is not None:
                if published_at is None:
                    continue
                if date_from is not None and published_at < date_from:
                    continue
                if date_to is not None and published_at > date_to:
                    continue
            results.append(
                (
                    int(item_id),
                    str(title),
                    str(published_at) if published_at is not None else "",
                )
            )
        url = _next_url(response.headers.get("Link"))
    return results


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
    form_data = read_tsv(tsv_file)

    date_from = (
        dt.datetime.strptime(args.date_from, "%d/%m/%y").date()
        if args.date_from
        else None
    )
    date_to = (
        dt.datetime.strptime(args.date_to, "%d/%m/%y").date() if args.date_to else None
    )

    print("Fetching all Layers...")
    layers = fetch_all(LAYERS, date_from, date_to)
    print(f"Fetched {len(layers)} layers")

    print("Fetching all Tables...")
    tables = fetch_all(TABLES, date_from, date_to)
    print(f"Fetched {len(tables)} tables")

    print("Fetching all Datasets...")
    datasets = fetch_all(DATASETS, date_from, date_to)
    print(f"Fetched {len(datasets)} datasets")

    all_items = layers + tables + datasets

    print("=== Layers/tables/datasets Publish status ===")
    for i, name, pub in all_items:
        if form_data.get(str(i).strip()) == "pub":
            print(f"ID {i}: {name} (first_published_at: {pub})")

    print()
    print("=== Layers/tables/datasets Publish With Restrictions ===")
    for i, name, pub in all_items:
        if form_data.get(str(i).strip()) == "pwr":
            print(f"ID {i}: {name} (first_published_at: {pub})")

    print()
    print("=== Layers/tables/datasets Do Not Publish ===")
    for i, name, pub in all_items:
        if form_data.get(str(i).strip()) == "dnp":
            print(f"ID {i}: {name} (first_published_at: {pub})")

    print()
    print("=== Data missing from NZGOAL Audit Form ===")
    missing_data = [
        (i, name, pub) for i, name, pub in all_items if str(i) not in form_data
    ]
    if not missing_data:
        print("(none)")
    else:
        for i, name, pub in missing_data:
            print(f"LAYER {i}: {name} (first_published_at: {pub})")


if __name__ == "__main__":
    sys.exit(main())
