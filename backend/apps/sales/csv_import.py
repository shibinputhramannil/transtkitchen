"""
Reading sales CSV files. Everything here works on the uploaded bytes only:
size, encoding, headers, and row count are all checked before anything is
stored.

Format (header names are matched ignoring case and surrounding spaces):

    date,item,quantity,item_id
    2026-11-02,Margherita Pizza,14,
    2026-11-02T19:30:00+05:30,Flat White,2,SKU-118

`date` is a kitchen-local date (YYYY-MM-DD), or an ISO date-time: with a UTC
offset it is converted to the kitchen's time zone, without one it is read as
kitchen-local time. `item_id` is optional; other columns are ignored.
"""

import csv
import hashlib
import io
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation

from apps.inventory.models import MAX_QUANTITY, QUANTITY_PLACES

MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_ROWS = 5000
REQUIRED_COLUMNS = ("date", "item", "quantity")
OPTIONAL_COLUMNS = ("item_id",)
MAX_ITEM_NAME = 200
MAX_ITEM_ID = 100
# Sales dated more than this far after the kitchen's today are mistakes.
MAX_DAYS_AHEAD = 1

DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
DATE_TIME = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?")

TEMPLATE = "date,item,quantity,item_id\n2026-11-02,Margherita Pizza,14,\n2026-11-02,Flat White,38,SKU-118\n"


class ImportFileError(Exception):
    """The file as a whole cannot be imported."""


@dataclass
class ParsedRow:
    row_number: int
    item_name: str = ""
    external_id: str = ""
    business_date: date | None = None
    sold_at: datetime | None = None
    quantity: Decimal | None = None
    errors: list = field(default_factory=list)

    @property
    def error(self):
        return "; ".join(self.errors)[:255]


@dataclass
class ParsedFile:
    sha256: str
    rows: list


def read_upload(upload):
    """Returns the uploaded file's bytes, refusing files over the size limit without reading them."""
    if upload.size > MAX_FILE_BYTES:
        raise ImportFileError(f"The file is larger than {MAX_FILE_BYTES // (1024 * 1024)} MB.")
    data = upload.read(MAX_FILE_BYTES + 1)
    if len(data) > MAX_FILE_BYTES:
        raise ImportFileError(f"The file is larger than {MAX_FILE_BYTES // (1024 * 1024)} MB.")
    return data


def parse_sales_csv(data, time_zone, today):
    """
    Parses a whole file into rows, each valid or carrying its errors.
    Raises ImportFileError when the file itself is unusable.
    """
    if not data.strip():
        raise ImportFileError("The file is empty.")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ImportFileError("The file is not UTF-8 text. Save it as “CSV UTF-8” and try again.") from None
    if "\x00" in text:
        raise ImportFileError("The file is not a text CSV file.")

    reader = csv.reader(io.StringIO(text, newline=""), strict=True)
    try:
        header = next(reader, None)
        if header is None:
            raise ImportFileError("The file is empty.")
        columns = read_header(header)

        rows = []
        for record in reader:
            if not any(cell.strip() for cell in record):
                continue  # blank line
            if len(rows) == MAX_ROWS:
                raise ImportFileError(f"The file has more than {MAX_ROWS} rows. Split it into smaller files.")
            # reader.line_num counts the header, so it is also the spreadsheet row number.
            rows.append(parse_row(reader.line_num, record, len(header), columns, time_zone, today))
    except csv.Error as error:
        raise ImportFileError(f"The file is not valid CSV (line {reader.line_num}): {error}.") from None

    if not rows:
        raise ImportFileError("The file has a header but no sales rows.")
    return ParsedFile(sha256=hashlib.sha256(data).hexdigest(), rows=rows)


def read_header(header):
    names = [name.strip().lower() for name in header]
    if any(not name for name in names):
        raise ImportFileError("Every column needs a header name; the file has an empty one.")
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise ImportFileError(f"Duplicate column headers: {', '.join(duplicates)}.")
    missing = [name for name in REQUIRED_COLUMNS if name not in names]
    if missing:
        raise ImportFileError(
            f"Missing required columns: {', '.join(missing)}. The header must include date, item, and quantity."
        )
    return {name: names.index(name) for name in (*REQUIRED_COLUMNS, *OPTIONAL_COLUMNS) if name in names}


def parse_row(row_number, record, width, columns, time_zone, today):
    row = ParsedRow(row_number=row_number)
    if len(record) != width:
        row.errors.append(f"Expected {width} columns but found {len(record)}.")
        return row

    def cell(name):
        return record[columns[name]].strip() if name in columns else ""

    row.item_name = cell("item")
    if not row.item_name:
        row.errors.append("The item is empty.")
    elif len(row.item_name) > MAX_ITEM_NAME:
        row.errors.append(f"The item name is longer than {MAX_ITEM_NAME} characters.")
        row.item_name = row.item_name[:MAX_ITEM_NAME]

    row.external_id = cell("item_id")
    if len(row.external_id) > MAX_ITEM_ID:
        row.errors.append(f"The item ID is longer than {MAX_ITEM_ID} characters.")
        row.external_id = row.external_id[:MAX_ITEM_ID]

    parse_date(row, cell("date"), time_zone, today)
    parse_quantity(row, cell("quantity"))
    return row


def parse_date(row, value, time_zone, today):
    try:
        if DATE.fullmatch(value):
            row.business_date = date.fromisoformat(value)
        elif DATE_TIME.fullmatch(value):
            moment = datetime.fromisoformat(value)
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=time_zone)  # kitchen-local time
            row.sold_at = moment
            row.business_date = moment.astimezone(time_zone).date()
        else:
            raise ValueError
    except ValueError:
        row.errors.append(f"“{value[:40]}” is not a date. Use YYYY-MM-DD or an ISO date-time.")
        return
    if row.business_date > today + timedelta(days=MAX_DAYS_AHEAD):
        row.errors.append(f"The date {row.business_date} is in the future.")
        row.business_date = None


def parse_quantity(row, value):
    try:
        quantity = Decimal(value)
    except InvalidOperation:
        row.errors.append(f"“{value[:20]}” is not a number.")
        return
    if not quantity.is_finite() or quantity <= 0:
        row.errors.append("The quantity must be a number greater than zero.")
    elif quantity.as_tuple().exponent < -QUANTITY_PLACES:
        row.errors.append(f"The quantity has more than {QUANTITY_PLACES} decimal places.")
    elif quantity > MAX_QUANTITY:
        row.errors.append("The quantity is too large.")
    else:
        row.quantity = quantity


def start_of_day(business_date, time_zone):
    """The first moment of a kitchen-local business date."""
    return datetime.combine(business_date, time.min, tzinfo=time_zone)
