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


def navigation_items_for_profile(items, business_type, tracking_modes=None):
    """Return menu entries valid for the selected profile and actual stock setup."""
    visible = list(items)
    if business_type != "auto_parts":
        visible = [item for item in visible if item[1] not in AUTO_PARTS_NAV_ITEMS]
    if tracking_modes is not None:
        modes = set(tracking_modes)
        if not modes.intersection({"batch", "expiry"}):
            visible = [item for item in visible if item[1] != "stock_batches"]
        if "serial" not in modes:
            visible = [item for item in visible if item[1] != "stock_serials"]
    return visible


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
