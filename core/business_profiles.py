"""Business-profile capabilities shared by navigation and request guards.

The current release intentionally shares core retail workflows across profiles.
Vehicle fitment/OE/analogue tools are the only genuinely automotive-specific
screens and must not leak into other store profiles through direct URLs.
"""
import re

AUTO_PARTS_NAV_ITEMS = frozenset({
    "car_parts", "catalogue_home", "crossrefs", "parts_import",
})

AUTO_PARTS_PATH_PREFIXES = (
    "/car-parts/",
    "/catalogue/",
    "/crossrefs/",
    "/parts-data/",
    "/part/",
    "/pos/car/",
)

AUTO_PARTS_PATH_PATTERNS = (
    re.compile(r"^/customers/\d+/car/add/$"),
    re.compile(r"^/cars/\d+/delete/$"),
)


def navigation_items_for_profile(items, business_type):
    """Return menu entries valid for the selected business profile."""
    if business_type == "auto_parts":
        return list(items)
    return [
        item for item in items
        if item[1] not in AUTO_PARTS_NAV_ITEMS
    ]


def required_profile_for_path(path):
    """Return the profile required by a specialized URL, if any."""
    normalized = "/" + (path or "").lstrip("/")
    if any(normalized.startswith(prefix) for prefix in AUTO_PARTS_PATH_PREFIXES):
        return "auto_parts"
    if any(pattern.fullmatch(normalized) for pattern in AUTO_PARTS_PATH_PATTERNS):
        return "auto_parts"
    return None


def profile_allows_path(business_type, path):
    required = required_profile_for_path(path)
    return required is None or business_type == required
