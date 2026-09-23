# ============================================================
# APPLE APP STORE REVIEW RESEARCH — STREAMLIT APP
#
# Run locally:
#     pip install streamlit pandas requests python-dateutil
#     streamlit run app.py
#
# Run on Streamlit Cloud:
#     push this file as app.py and deploy.
#
# Pipeline:
#     App ID
#         ↓
#     Multiple storefronts
#         ↓
#     Apple public customer reviews RSS endpoint
#         ↓
#     JSON → XML fallback
#         ↓
#     Pagination
#         ↓
#     Normalization
#         ↓
#     Deduplication
#         ↓
#     Downloadable CSV
#
# IMPORTANT:
# Apple's public customer-review endpoint does NOT guarantee access
# to the complete historical review database.
#
# The app collects all reviews available through the public
# endpoint for each selected storefront, within MAX_PAGES.
#
# It must NOT be interpreted as collecting every historical review
# ever published for an app.
# ============================================================


# ============================================================
# CONFIGURATION
# ============================================================

MAX_PAGES = 10

REQUEST_DELAY = 1
MAX_RETRIES = 3
REQUEST_TIMEOUT = 20

CSV_SEPARATOR = ","

DEFAULT_STOREFRONT = "us"

DEBUG = False


# ============================================================
# IMPORTS
# ============================================================

import io
import re
import time
import csv
import xml.etree.ElementTree as ET

from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import requests
import streamlit as st

from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from dateutil import parser as date_parser


# ============================================================
# COUNTRY / STOREFRONT MAP
# ============================================================

COUNTRY_NAMES = {
    "us": "United States",
    "ru": "Russia",
    "gb": "United Kingdom",
    "de": "Germany",
    "fr": "France",
    "it": "Italy",
    "es": "Spain",
    "ca": "Canada",
    "au": "Australia",
    "jp": "Japan",
    "kr": "South Korea",
    "br": "Brazil",
    "mx": "Mexico",
    "nl": "Netherlands",
    "se": "Sweden",
    "no": "Norway",
    "fi": "Finland",
    "pl": "Poland",
    "tr": "Turkey",
    "in": "India",
}


# ============================================================
# FINAL CSV SCHEMA
# ============================================================

CSV_COLUMNS = [
    "review_id",
    "country_code",
    "country_name",
    "storefront",
    "app_id",
    "app_name",
    "rating",
    "title",
    "review",
    "author",
    "review_date",
    "app_version",
    "vote_count",
    "author_url",
    "review_url",
    "source_url",
    "page",
]


# ============================================================
# DEBUG
# ============================================================

def debug(message: str) -> None:
    """
    Print technical information only when DEBUG=True.
    """
    if DEBUG:
        print(f"[DEBUG] {message}")


# ============================================================
# HTTP SESSION
# ============================================================

def create_session() -> requests.Session:
    """
    Create a requests session with retry and a reasonable User-Agent.
    """

    session = requests.Session()

    retry = Retry(
        total=MAX_RETRIES,
        connect=MAX_RETRIES,
        read=MAX_RETRIES,
        status=MAX_RETRIES,
        backoff_factor=1,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET"]),
        raise_on_status=False,
    )

    adapter = HTTPAdapter(max_retries=retry)

    session.mount("https://", adapter)

    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/149.0 Safari/537.36"
        ),
        "Accept": (
            "application/json, "
            "application/xml, "
            "text/xml, "
            "*/*"
        ),
    })

    return session


# ============================================================
# APP ID
# ============================================================

def parse_app_id(raw: str) -> str:
    """
    Validate and normalize an App Store application ID.

    Accepts:
        - digits only (e.g. 123456789)
        - a full App Store URL (the ID is extracted)

    Returns a string containing only digits.
    """

    raw = (raw or "").strip()

    if not raw:
        raise ValueError(
            "App ID не указан. Введите числовой ID приложения."
        )

    # --------------------------------------------------------
    # URL pasted — extract ID from it
    # --------------------------------------------------------

    if raw.lower().startswith("http") or "apps.apple.com" in raw.lower():

        match = re.search(
            r"/id(\d+)(?:[/?#]|$)",
            raw,
            flags=re.IGNORECASE,
        )

        if not match:
            raise ValueError(
                "Не удалось определить App Store ID из ссылки."
            )

        return match.group(1)

    # --------------------------------------------------------
    # Plain numeric ID
    # --------------------------------------------------------

    if not re.fullmatch(r"\d+", raw):
        raise ValueError(
            "App ID должен состоять только из цифр. "
            "Например: 123456789"
        )

    return raw


# ============================================================
# STOREFRONT NORMALIZATION
# ============================================================

def normalize_storefronts(
    raw: str,
    fallback: Optional[str],
) -> List[str]:
    """
    Normalize storefront input.

    Examples:
        "US, RU, GB"  →  ["us", "ru", "gb"]
    """

    values = [
        item.strip().lower()
        for item in (raw or "").split(",")
        if item.strip()
    ]

    # --------------------------------------------------------
    # Empty input → fallback storefront
    # --------------------------------------------------------

    if not values:

        if not fallback:
            raise ValueError(
                "Не указан storefront. "
                "Введите хотя бы один storefront."
            )

        values = [fallback.lower()]

    normalized: List[str] = []
    invalid: List[str] = []

    for value in values:

        if not re.fullmatch(r"[a-z]{2}", value):

            invalid.append(value)

        elif value not in normalized:

            normalized.append(value)

    if invalid:

        raise ValueError(
            "Некорректный storefront: "
            + ", ".join(invalid)
            + ". Используйте ровно две буквы, "
              "например: us, ru, gb."
        )

    return normalized


# ============================================================
# COUNTRY NAME
# ============================================================

def get_country_name(storefront: str) -> str:
    """
    Return country name for known storefront.
    Unknown storefronts are preserved and receive "Unknown".
    """

    return COUNTRY_NAMES.get(storefront.lower(), "Unknown")


# ============================================================
# SOURCE APP STORE URL
# ============================================================

def get_source_url(app_id: str, storefront: str) -> str:
    """
    Build storefront-specific App Store URL.
    """

    return f"https://apps.apple.com/{storefront}/app/id{app_id}"


# ============================================================
# APP METADATA
# ============================================================

def get_app_metadata(
    session: requests.Session,
    app_id: str,
    storefront: str,
    fallback_name: str = "Unknown",
) -> str:
    """
    Get localized application name via iTunes Lookup API.
    """

    url = (
        f"https://itunes.apple.com/lookup"
        f"?id={app_id}"
        f"&country={storefront}"
    )

    debug(f"Metadata URL: {url}")

    try:

        response = session.get(url, timeout=REQUEST_TIMEOUT)

        debug(f"Metadata status: {response.status_code}")

        response.raise_for_status()

        data = response.json()

        results = data.get("results") or []

        if results:

            track_name = results[0].get("trackName")

            if track_name:

                return str(track_name).strip()

    except Exception as exc:

        debug(f"Metadata error: {exc}")

    return fallback_name or "Unknown"


# ============================================================
# REVIEW URLS
# ============================================================

def build_review_urls(
    app_id: str,
    storefront: str,
    page: int,
) -> Tuple[str, str]:
    """
    Build JSON and XML review endpoints.
    """

    base = (
        f"https://itunes.apple.com/"
        f"{storefront}/rss/customerreviews/"
        f"page={page}/"
        f"id={app_id}/"
        f"sortby=mostrecent"
    )

    return f"{base}/json", f"{base}/xml"


# ============================================================
# JSON HELPERS
# ============================================================

def unwrap_label(value: Any) -> str:
    """
    Apple RSS JSON commonly stores values as {"label": "value"}.
    """

    if isinstance(value, dict):

        value = value.get("label", "")

    if value is None:

        return ""

    return str(value)


def first_value(entry: Dict[str, Any], *keys: str) -> str:
    """
    Return the first non-empty value from the supplied keys.
    """

    for key in keys:

        if key in entry:

            value = unwrap_label(entry[key])

            if value:

                return value

    return ""


def normalize_entries(entries: Any) -> List[Dict[str, Any]]:
    """
    Apple can return one object instead of a list.
    Normalize both cases into a list.
    """

    if not entries:

        return []

    if isinstance(entries, dict):

        return [entries]

    if isinstance(entries, list):

        return [item for item in entries if isinstance(item, dict)]

    return []


# ============================================================
# FETCH JSON
# ============================================================

def fetch_json_reviews(
    session: requests.Session,
    url: str,
) -> Tuple[str, Optional[List[Dict[str, Any]]], Optional[str]]:

    debug(f"GET JSON: {url}")

    try:

        response = session.get(url, timeout=REQUEST_TIMEOUT)

        debug(f"JSON status: {response.status_code}")

        if response.status_code in (403, 404):

            return ("HTTP_ERROR", None, str(response.status_code))

        if response.status_code >= 500:

            return ("HTTP_ERROR", None, str(response.status_code))

        if response.status_code != 200:

            return ("HTTP_ERROR", None, str(response.status_code))

        try:

            data = response.json()

        except ValueError as exc:

            return ("PARSE_ERROR", None, f"JSON decode: {exc}")

        feed = data.get("feed", {})

        entries = normalize_entries(feed.get("entry"))

        return ("SUCCESS", entries, None)

    except requests.RequestException as exc:

        return ("HTTP_ERROR", None, str(exc))


# ============================================================
# XML HELPERS
# ============================================================

def xml_text(element: Optional[ET.Element]) -> str:

    if element is None:

        return ""

    return "".join(element.itertext()).strip()


def local_name(tag: str) -> str:
    """
    {namespace}rating → rating
    """

    return tag.rsplit("}", 1)[-1]


def child_by_local_name(
    element: ET.Element,
    name: str,
) -> Optional[ET.Element]:

    for child in list(element):

        if local_name(child.tag) == name:

            return child

    return None


def children_by_local_name(
    element: ET.Element,
    name: str,
) -> List[ET.Element]:

    return [
        child
        for child in list(element)
        if local_name(child.tag) == name
    ]


def xml_child_text(element: ET.Element, *names: str) -> str:

    wanted = set(names)

    for child in list(element):

        if local_name(child.tag) in wanted:

            text = xml_text(child)

            if text:

                return text

    return ""


# ============================================================
# FETCH XML
# ============================================================

def fetch_xml_reviews(
    session: requests.Session,
    url: str,
) -> Tuple[str, Optional[List[Dict[str, Any]]], Optional[str]]:

    debug(f"GET XML: {url}")

    try:

        response = session.get(url, timeout=REQUEST_TIMEOUT)

        debug(f"XML status: {response.status_code}")

        if response.status_code in (403, 404):

            return ("HTTP_ERROR", None, str(response.status_code))

        if response.status_code >= 500:

            return ("HTTP_ERROR", None, str(response.status_code))

        if response.status_code != 200:

            return ("HTTP_ERROR", None, str(response.status_code))

        try:

            root = ET.fromstring(response.content)

        except ET.ParseError as exc:

            return ("PARSE_ERROR", None, f"XML parse: {exc}")

        entries = []

        for entry in root.iter():

            if local_name(entry.tag) == "entry":

                entries.append({"_xml_element": entry})

        return ("SUCCESS", entries, None)

    except requests.RequestException as exc:

        return ("HTTP_ERROR", None, str(exc))


# ============================================================
# PARSE JSON REVIEWS
# ============================================================

def parse_json_reviews(
    entries: List[Dict[str, Any]],
    storefront: str,
    app_id: str,
    app_name: str,
    source_url: str,
    page: int,
) -> List[Dict[str, Any]]:

    reviews: List[Dict[str, Any]] = []

    for entry in entries:

        rating = first_value(entry, "im:rating", "rating")

        if not rating:

            continue

        review_id = first_value(entry, "id", "reviewId")

        title = first_value(entry, "title", "review_title")

        review = first_value(entry, "content", "review", "review_text")

        author = ""

        author_obj = entry.get("author")

        if isinstance(author_obj, dict):

            author = unwrap_label(author_obj.get("name"))

        author = author or first_value(entry, "author")

        author_url = ""

        if isinstance(author_obj, dict):

            author_url = unwrap_label(author_obj.get("uri"))

        review_date = first_value(
            entry, "updated", "review_date", "date",
        )

        app_version = first_value(
            entry, "im:version", "version", "app_version",
        )

        vote_count = first_value(
            entry, "im:voteSum", "vote_count",
        )

        review_url = ""

        links = entry.get("link")

        if isinstance(links, dict):

            links = [links]

        if isinstance(links, list):

            for link in links:

                if not isinstance(link, dict):
                    continue

                attrs = link.get("attributes") or {}

                href = attrs.get("href", "")

                rel = attrs.get("rel", "")

                if href and (rel in ("related", "alternate", "") or not review_url):

                    review_url = href

                    if rel == "related":

                        break

        reviews.append({
            "review_id": review_id,
            "country_code": storefront.upper(),
            "country_name": get_country_name(storefront),
            "storefront": storefront,
            "app_id": app_id,
            "app_name": app_name or "Unknown",
            "rating": rating,
            "title": title,
            "review": review,
            "author": author,
            "review_date": review_date,
            "app_version": app_version,
            "vote_count": vote_count,
            "author_url": author_url,
            "review_url": review_url,
            "source_url": source_url,
            "page": page,
        })

    return reviews


# ============================================================
# PARSE XML REVIEWS
# ============================================================

def parse_xml_reviews(
    entries: List[Dict[str, Any]],
    storefront: str,
    app_id: str,
    app_name: str,
    source_url: str,
    page: int,
) -> List[Dict[str, Any]]:

    reviews: List[Dict[str, Any]] = []

    for wrapper in entries:

        entry = wrapper.get("_xml_element")

        if entry is None:

            continue

        rating = xml_child_text(entry, "rating")

        if not rating:

            continue

        review_id = xml_child_text(entry, "id")

        title = xml_child_text(entry, "title")

        review = xml_child_text(entry, "content")

        review_date = xml_child_text(entry, "updated")

        app_version = xml_child_text(entry, "version")

        vote_count = xml_child_text(entry, "voteSum")

        author = ""
        author_url = ""

        author_element = child_by_local_name(entry, "author")

        if author_element is not None:

            author = xml_child_text(author_element, "name")

            author_url = xml_child_text(author_element, "uri")

        review_url = ""

        for link in children_by_local_name(entry, "link"):

            href = link.attrib.get("href", "")

            rel = link.attrib.get("rel", "")

            if href and (rel == "related" or not review_url):

                review_url = href

                if rel == "related":

                    break

        reviews.append({
            "review_id": review_id,
            "country_code": storefront.upper(),
            "country_name": get_country_name(storefront),
            "storefront": storefront,
            "app_id": app_id,
            "app_name": app_name or "Unknown",
            "rating": rating,
            "title": title,
            "review": review,
            "author": author,
            "review_date": review_date,
            "app_version": app_version,
            "vote_count": vote_count,
            "author_url": author_url,
            "review_url": review_url,
            "source_url": source_url,
            "page": page,
        })

    return reviews


# ============================================================
# COLLECT ONE STOREFRONT
# ============================================================

def collect_reviews_for_storefront(
    session: requests.Session,
    app_id: str,
    app_name: str,
    storefront: str,
    source_url: str,
    progress_callback: Optional[Any] = None,
) -> Tuple[List[Dict[str, Any]], str]:
    """
    Collect reviews for one storefront.

    JSON is tried first. XML is used as fallback.

    progress_callback(page, count) is called after each page
    if provided.
    """

    all_reviews: List[Dict[str, Any]] = []

    status = "SUCCESS"

    for page in range(1, MAX_PAGES + 1):

        json_url, xml_url = build_review_urls(app_id, storefront, page)

        # ----------------------------------------------------
        # JSON
        # ----------------------------------------------------

        (
            json_status,
            json_entries,
            json_error,
        ) = fetch_json_reviews(session, json_url)

        if json_status == "SUCCESS" and json_entries is not None:

            page_reviews = parse_json_reviews(
                json_entries,
                storefront,
                app_id,
                app_name,
                source_url,
                page,
            )

            endpoint_used = "JSON"

        else:

            # ------------------------------------------------
            # XML fallback
            # ------------------------------------------------

            (
                xml_status,
                xml_entries,
                xml_error,
            ) = fetch_xml_reviews(session, xml_url)

            if xml_status == "SUCCESS" and xml_entries is not None:

                page_reviews = parse_xml_reviews(
                    xml_entries,
                    storefront,
                    app_id,
                    app_name,
                    source_url,
                    page,
                )

                endpoint_used = "XML"

            else:

                debug(f"XML unavailable: {xml_error}")

                if page == 1:

                    if (
                        json_status == "HTTP_ERROR"
                        and xml_status == "HTTP_ERROR"
                    ):

                        status = "ENDPOINT_UNAVAILABLE"

                    elif (
                        json_status == "PARSE_ERROR"
                        or xml_status == "PARSE_ERROR"
                    ):

                        status = "PARSE_ERROR"

                    else:

                        status = "HTTP_ERROR"

                break

        debug(
            f"{storefront} page={page} "
            f"endpoint={endpoint_used} "
            f"reviews={len(page_reviews)}"
        )

        if progress_callback:

            progress_callback(page, len(page_reviews))

        # ----------------------------------------------------
        # Empty page → stop
        # ----------------------------------------------------

        if not page_reviews:

            if not all_reviews:

                status = "NO_REVIEWS"

            break

        all_reviews.extend(page_reviews)

        if page < MAX_PAGES:

            time.sleep(REQUEST_DELAY)

    if all_reviews:

        status = "SUCCESS"

    return all_reviews, status


# ============================================================
# COLLECT ALL STOREFRONTS
# ============================================================

def collect_reviews_for_storefronts(
    session: requests.Session,
    app_id: str,
    storefronts: List[str],
    progress_callback: Optional[Any] = None,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:

    all_reviews: List[Dict[str, Any]] = []

    summary: List[Dict[str, Any]] = []

    for storefront in storefronts:

        source_url = get_source_url(app_id, storefront)

        app_name = get_app_metadata(
            session,
            app_id,
            storefront,
            fallback_name="Unknown",
        )

        if progress_callback:

            progress_callback(storefront, app_name, "start", 0)

        try:

            reviews, status = collect_reviews_for_storefront(
                session=session,
                app_id=app_id,
                app_name=app_name,
                storefront=storefront,
                source_url=source_url,
                progress_callback=(
                    lambda page, count, sf=storefront, an=app_name:
                    progress_callback(sf, an, "page", page, count)
                    if progress_callback else None
                ),
            )

        except Exception as exc:

            reviews = []

            status = "HTTP_ERROR"

            debug(f"Unexpected storefront error: {exc}")

        all_reviews.extend(reviews)

        summary.append({
            "storefront": storefront,
            "country_code": storefront.upper(),
            "country_name": get_country_name(storefront),
            "app_name": app_name,
            "status": status,
            "count": len(reviews),
        })

        if progress_callback:

            progress_callback(
                storefront,
                app_name,
                "done",
                status,
                len(reviews),
            )

    return all_reviews, summary


# ============================================================
# REVIEW UNIQUE KEY
# ============================================================

def create_review_key(review: Dict[str, Any]) -> Tuple[Any, ...]:

    review_id = str(review.get("review_id", "")).strip()

    storefront = str(review.get("storefront", "")).strip().lower()

    if review_id:

        return ("id", review_id, storefront)

    return (
        "fallback",
        storefront,
        str(review.get("review_date", "")).strip(),
        str(review.get("author", "")).strip(),
        str(review.get("title", "")).strip(),
        str(review.get("review", "")).strip(),
    )


# ============================================================
# DEDUPLICATION
# ============================================================

def deduplicate_reviews(
    reviews: List[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], int]:

    seen = set()

    unique: List[Dict[str, Any]] = []

    for review in reviews:

        key = create_review_key(review)

        if key in seen:

            continue

        seen.add(key)

        unique.append(review)

    return unique, len(reviews) - len(unique)


# ============================================================
# NORMALIZATION
# ============================================================

def normalize_reviews(
    reviews: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:

    normalized = []

    for review in reviews:

        item = {
            column: review.get(column, "")
            for column in CSV_COLUMNS
        }

        item["storefront"] = str(item["storefront"]).lower().strip()

        if item["country_code"]:

            item["country_code"] = (
                str(item["country_code"]).upper().strip()
            )

        else:

            item["country_code"] = item["storefront"].upper()

        normalized.append(item)

    return normalized


# ============================================================
# DATE PARSING
# ============================================================

def parse_date_for_sort(value: Any) -> datetime:

    if not value:

        return datetime.min

    try:

        dt = date_parser.parse(str(value))

        if dt.tzinfo is not None:

            dt = dt.replace(tzinfo=None)

        return dt

    except (ValueError, TypeError, OverflowError):

        return datetime.min


# ============================================================
# SORTING
# ============================================================

def sort_reviews(
    reviews: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:

    return sorted(
        reviews,
        key=lambda review: (
            parse_date_for_sort(review.get("review_date")),
            str(review.get("storefront", "")),
        ),
        reverse=True,
    )


# ============================================================
# STATISTICS
# ============================================================

def create_statistics(
    reviews: List[Dict[str, Any]],
    summary: List[Dict[str, Any]],
) -> Dict[str, Any]:

    df = pd.DataFrame(reviews, columns=CSV_COLUMNS)

    rating_counts = {str(i): 0 for i in range(1, 6)}

    if not df.empty:

        ratings = pd.to_numeric(df["rating"], errors="coerce").dropna()

        ratings = ratings.astype(int)

        for rating, count in ratings.value_counts().items():

            if int(rating) in range(1, 6):

                rating_counts[str(int(rating))] = int(count)

    dates = (
        [parse_date_for_sort(v) for v in df["review_date"]]
        if not df.empty
        else []
    )

    valid_dates = [d for d in dates if d != datetime.min]

    version_values = []

    if not df.empty:

        version_values = [
            str(v).strip()
            for v in df["app_version"].tolist()
            if str(v).strip() and str(v).strip().lower() != "nan"
        ]

    return {
        "total": len(reviews),
        "by_storefront": {
            item["storefront"]: item["count"]
            for item in summary
        },
        "rating_counts": rating_counts,
        "earliest": (
            min(valid_dates).date().isoformat()
            if valid_dates
            else "Unknown"
        ),
        "latest": (
            max(valid_dates).date().isoformat()
            if valid_dates
            else "Unknown"
        ),
        "versions": sorted(set(version_values)),
    }


# ============================================================
# CSV SERIALIZATION
# ============================================================

def reviews_to_csv_bytes(
    reviews: List[Dict[str, Any]],
) -> bytes:
    """
    Serialize normalized reviews to UTF-8-SIG CSV bytes
    for st.download_button.
    """

    df = pd.DataFrame(reviews, columns=CSV_COLUMNS)

    buffer = io.StringIO()

    df.to_csv(
        buffer,
        index=False,
        sep=CSV_SEPARATOR,
        quoting=csv.QUOTE_ALL,
        lineterminator="\n",
    )

    # utf-8-sig so Excel opens Cyrillic/emoji correctly
    return buffer.getvalue().encode("utf-8-sig")


# ============================================================
# STREAMLIT UI
# ============================================================

st.set_page_config(
    page_title="Apple App Store Review Research",
    page_icon="🍎",
    layout="wide",
)


def render_header() -> None:

    st.title("🍎 Apple App Store Review Research")

    st.markdown(
        """
        Сбор отзывов из публичного RSS-эндпоинта Apple
        по нескольким storefront'ам.

        **Важно:** публичный эндпоинт Apple не гарантирует
        доступ ко всей исторической базе отзывов. Для некоторых
        storefront может быть доступен только ограниченный
        набор наиболее свежих отзывов.
        """
    )

    with st.expander("Как это работает"):

        st.markdown(
            f"""
            1. Вы вводите **App ID** (число, например `123456789`).
            2. Указываете storefront'ы через запятую (`us, ru, gb`).
            3. Для каждого storefront скрипт проходит по страницам
               публичного RSS-эндпоинта Apple от `page=1` до `page={MAX_PAGES}`,
               останавливаясь на первой пустой странице.
            4. JSON-эндпоинт используется как основной,
               XML — как резервный.
            5. Результаты нормализуются, дедуплицируются
               и объединяются в один CSV.
            """
        )


def render_sidebar() -> Dict[str, Any]:

    st.sidebar.header("Параметры")

    app_id_input = st.sidebar.text_input(
        "App ID или ссылка App Store",
        value="",
        placeholder="123456789",
        help="Можно вставить числовой ID или полную ссылку.",
    )

    storefronts_input = st.sidebar.text_input(
        "Storefront'ы (через запятую)",
        value=DEFAULT_STOREFRONT,
        help="Например: us, ru, gb, de, fr",
    )

    st.sidebar.markdown("---")

    st.sidebar.caption(
        f"MAX_PAGES = {MAX_PAGES}\n\n"
        f"REQUEST_DELAY = {REQUEST_DELAY} сек\n\n"
        f"MAX_RETRIES = {MAX_RETRIES}\n\n"
        f"TIMEOUT = {REQUEST_TIMEOUT} сек"
    )

    start = st.sidebar.button(
        "🚀 Собрать отзывы",
        type="primary",
        use_container_width=True,
    )

    clear = st.sidebar.button(
        "🗑 Очистить результаты",
        use_container_width=True,
    )

    return {
        "app_id": app_id_input,
        "storefronts": storefronts_input,
        "start": start,
        "clear": clear,
    }


def clear_results() -> None:
    """
    Remove previously collected results from session state.
    """

    for key in (
        "reviews_df",
        "summary",
        "stats",
        "csv_bytes",
        "app_id",
        "app_name",
        "storefronts",
        "duplicates_removed",
        "total_before_dedup",
        "output_filename",
    ):

        st.session_state.pop(key, None)


def run_collection(app_id_input: str, storefronts_input: str) -> None:
    """
    Full pipeline with live Streamlit status updates.
    """

    # --------------------------------------------------------
    # Validate app ID
    # --------------------------------------------------------

    try:

        app_id = parse_app_id(app_id_input)

    except ValueError as exc:

        st.error(str(exc))

        return

    # --------------------------------------------------------
    # Validate storefronts
    # --------------------------------------------------------

    try:

        storefronts = normalize_storefronts(
            storefronts_input,
            fallback=DEFAULT_STOREFRONT,
        )

    except ValueError as exc:

        st.error(str(exc))

        return

    st.info(
        f"App ID: **{app_id}**  \n"
        f"Storefront'ы: **{', '.join(storefronts)}**"
    )

    session = create_session()

    # --------------------------------------------------------
    # Initial metadata
    # --------------------------------------------------------

    with st.spinner("Получаем метаданные приложения…"):

        initial_app_name = get_app_metadata(
            session,
            app_id,
            storefronts[0],
            fallback_name="Unknown",
        )

    st.success(f"Приложение: **{initial_app_name}**")

    # --------------------------------------------------------
    # Progress widgets
    # --------------------------------------------------------

    progress_bar = st.progress(0.0, text="Подготовка…")

    log_container = st.container()

    def progress_callback(*args: Any) -> None:
        """
        Receive progress events from collection functions.

        Events:
            (storefront, app_name, "start", 0)
            (storefront, app_name, "page", page, count)
            (storefront, app_name, "done", status, count)
        """

        if not args:

            return

        event = args[2] if len(args) >= 3 else ""

        if event == "start":

            sf = args[0]

            progress_bar.progress(
                0.0,
                text=f"Собираем storefront={sf}…",
            )

        elif event == "page":

            sf, an, _, page, count = args[:5]

            log_container.write(
                f"`{sf.upper()}` — page {page}: "
                f"{count} отзывов"
            )

        elif event == "done":

            sf, an, _, status, count = args[:5]

            log_container.write(
                f"✅ `{sf.upper()}` завершён: "
                f"{status} ({count} отзывов)"
            )

    # --------------------------------------------------------
    # Collect
    # --------------------------------------------------------

    try:

        new_reviews, summary = collect_reviews_for_storefronts(
            session=session,
            app_id=app_id,
            storefronts=storefronts,
            progress_callback=progress_callback,
        )

    except Exception as exc:

        st.error(f"Ошибка при сборе: {exc}")

        return

    progress_bar.progress(1.0, text="Обработка…")

    # --------------------------------------------------------
    # Normalize + deduplicate + sort
    # --------------------------------------------------------

    total_before_dedup = len(new_reviews)

    normalized = normalize_reviews(new_reviews)

    unique, duplicates_removed = deduplicate_reviews(normalized)

    sorted_reviews = sort_reviews(unique)

    stats = create_statistics(sorted_reviews, summary)

    # --------------------------------------------------------
    # Save to session state
    # --------------------------------------------------------

    st.session_state["reviews_df"] = pd.DataFrame(
        sorted_reviews,
        columns=CSV_COLUMNS,
    )

    st.session_state["summary"] = summary
    st.session_state["stats"] = stats
    st.session_state["csv_bytes"] = reviews_to_csv_bytes(sorted_reviews)
    st.session_state["app_id"] = app_id
    st.session_state["app_name"] = initial_app_name
    st.session_state["storefronts"] = storefronts
    st.session_state["duplicates_removed"] = duplicates_removed
    st.session_state["total_before_dedup"] = total_before_dedup
    st.session_state["output_filename"] = f"app_reviews_{app_id}.csv"

    st.success(
        f"Готово: собрано **{total_before_dedup}** отзывов, "
        f"уникальных — **{stats['total']}**, "
        f"дубликатов удалено — **{duplicates_removed}**."
    )


def render_summary() -> None:
    """
    Render collected summary, statistics and download button.
    """

    if "reviews_df" not in st.session_state:

        return

    df: pd.DataFrame = st.session_state["reviews_df"]
    summary: List[Dict[str, Any]] = st.session_state["summary"]
    stats: Dict[str, Any] = st.session_state["stats"]

    app_id = st.session_state["app_id"]
    app_name = st.session_state["app_name"]
    storefronts = st.session_state["storefronts"]
    duplicates_removed = st.session_state["duplicates_removed"]
    total_before_dedup = st.session_state["total_before_dedup"]
    output_filename = st.session_state["output_filename"]
    csv_bytes: bytes = st.session_state["csv_bytes"]

    st.markdown("---")
    st.header("Результаты")

    # --------------------------------------------------------
    # Top metrics
    # --------------------------------------------------------

    col1, col2, col3, col4 = st.columns(4)

    col1.metric("App ID", app_id)
    col2.metric("Собрано", total_before_dedup)
    col3.metric("Уникальных", stats["total"])
    col4.metric("Дубликатов", duplicates_removed)

    # --------------------------------------------------------
    # Collection summary
    # --------------------------------------------------------

    st.subheader("Сводка по storefront'ам")

    summary_df = pd.DataFrame(summary)[
        ["country_code", "storefront", "country_name",
         "app_name", "status", "count"]
    ]

    st.dataframe(summary_df, use_container_width=True)

    # --------------------------------------------------------
    # Rating distribution
    # --------------------------------------------------------

    st.subheader("Распределение оценок")

    rating_df = pd.DataFrame({
        "rating": list(stats["rating_counts"].keys()),
        "count": list(stats["rating_counts"].values()),
    }).set_index("rating")

    st.bar_chart(rating_df)

    # --------------------------------------------------------
    # Date range and versions
    # --------------------------------------------------------

    col1, col2 = st.columns(2)

    col1.markdown(
        f"**Диапазон дат:** "
        f"{stats['earliest']} — {stats['latest']}"
    )

    if stats["versions"]:

        col2.markdown(
            "**Версии приложения:** "
            + ", ".join(stats["versions"][:20])
            + ("…" if len(stats["versions"]) > 20 else "")
        )

    # --------------------------------------------------------
    # Download button
    # --------------------------------------------------------

    st.subheader("Скачать CSV")

    st.download_button(
        label="⬇️ Скачать CSV",
        data=csv_bytes,
        file_name=output_filename,
        mime="text/csv",
        use_container_width=True,
    )

    # --------------------------------------------------------
    # Reviews table
    # --------------------------------------------------------

    st.subheader("Отзывы")

    st.caption(
        f"Строк: {len(df)}. "
        "Сортировка: сначала новые по дате отзыва."
    )

    st.dataframe(
        df,
        use_container_width=True,
        height=600,
    )


def main() -> None:

    render_header()

    params = render_sidebar()

    if params["clear"]:

        clear_results()

        st.rerun()

    if params["start"]:

        run_collection(
            app_id_input=params["app_id"],
            storefronts_input=params["storefronts"],
        )

    render_summary()


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":

    main()