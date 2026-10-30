import ipaddress
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation


def parse_amount(value):
    try:
        amount = Decimal(value)
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not amount.is_finite() or amount <= 0 or amount > Decimal("1000000"):
        return None
    return amount.quantize(Decimal("0.01"))


def parse_local_datetime(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return False
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def valid_host(value):
    value = value.strip()
    candidate = value[1:-1] if value.startswith("[") and value.endswith("]") else value
    try:
        ipaddress.ip_address(candidate)
        return candidate
    except ValueError:
        if len(candidate) > 253 or not candidate:
            return None
        labels = candidate.rstrip(".").split(".")
        if all(
            label
            and len(label) <= 63
            and re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?", label)
            for label in labels
        ):
            return candidate
        return None


def valid_address_ranges(value):
    value = value.strip()
    if not value:
        return ""
    entries = [entry.strip() for entry in value.split(",")]
    if any(not entry for entry in entries):
        return None
    for entry in entries:
        try:
            ipaddress.ip_network(entry, strict=False)
        except ValueError:
            if valid_host(entry) is None:
                return None
    return ",".join(entries)


def address_matches_ranges(address, ranges):
    try:
        candidate_ip = ipaddress.ip_address(address)
    except ValueError:
        candidate_ip = None
    for entry in (ranges or "").split(","):
        entry = entry.strip()
        if not entry:
            continue
        try:
            network = ipaddress.ip_network(entry, strict=False)
        except ValueError:
            if candidate_ip is None and address.rstrip(".").casefold() == entry.rstrip(".").casefold():
                return True
        else:
            if candidate_ip is not None and candidate_ip in network:
                return True
    return False
