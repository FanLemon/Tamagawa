import uvicorn
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, HTTPException, status, Depends, Query, Header
from fastapi.responses import HTMLResponse
from sqlmodel import Field, Session, SQLModel, create_engine, select, func
from sqlalchemy.exc import SQLAlchemyError
import okcard_storage
from decimal import Decimal
from typing import List, Union
from typing_extensions import Annotated
import asyncio
import base64
import datetime
import html as html_lib
import http.client
import json
import os
import re
import threading
import urllib.error
import urllib.request


BOCD_EXCHANGE_RATE_URL = "https://www.bocd.com.cn/icms/paijia/gjyw.html"

# OKCard (OK store app) — same flow as okcard-history.swift: log in for a
# Firebase token, then pull the full purchase history and filter by month.
# Credentials come from the environment (set OKCARD_USER / OKCARD_PASS).
OKCARD_BASE_URL   = "https://okapp.ok-corporation.co.jp"
OKCARD_APP_USER   = "okapp"             # static app-gate (HTTP Basic) username
OKCARD_APP_PASS   = "hs6sfeja8"         # static app-gate (HTTP Basic) password
OKCARD_USER_AGENT = "okstore v1.0.0) "  # keep exactly as-is (matches the app)
OKCARD_HTML_TEMPLATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "okcard-history.html")




class BocdExchangeRate(SQLModel, table=True):
    __tablename__ = "bocd_exchange_rates"
    __table_args__ = {'extend_existing': True}

    id: Union[int, None] = Field(default=None, primary_key=True, nullable=False)
    currency: str = Field(index=False)
    cash_buy: Decimal = Field(max_digits=12, decimal_places=4)
    fx_buy: Decimal = Field(max_digits=12, decimal_places=4)
    sell: Decimal = Field(max_digits=12, decimal_places=4)
    middle: Decimal = Field(max_digits=12, decimal_places=4)
    published_at: datetime.datetime = Field(index=False)
    source_url: str = Field(default=BOCD_EXCHANGE_RATE_URL)
    fetched_at: datetime.datetime = Field(default_factory=datetime.datetime.utcnow)
    

# Database settings come from the environment (DATABASE_URL in .env; see README).
DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL not set; load .env first (see README step 1)")

psql_engine = create_engine(DATABASE_URL)


def create_db_and_tables():
    SQLModel.metadata.create_all(psql_engine)


def get_session():
    with Session(psql_engine) as session:
        yield session


SessionDep = Annotated[Session, Depends(get_session)]


def parse_chinese_datetime(value: str) -> datetime.datetime:
    match = re.search(r"(\d{4})年(\d{1,2})月(\d{1,2})日(\d{1,2})时(\d{1,2})分", value)
    if not match:
        raise ValueError("BOCD published date not found")

    year, month, day, hour, minute = [int(part) for part in match.groups()]
    return datetime.datetime(year, month, day, hour, minute)


def strip_html(html: str) -> str:
    text = re.sub(r"<script\b[^>]*>.*?</script>", " ", html, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<style\b[^>]*>.*?</style>", " ", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html_lib.unescape(text)
    return re.sub(r"\s+", " ", text)


def parse_bocd_exchange_rates(html: str) -> List[BocdExchangeRate]:
    text = strip_html(html)
    published_at = parse_chinese_datetime(text)
    rows = []
    pattern = re.compile(
        r"(英镑|澳元|欧元|日元|美元|港币)\s+"
        r"([0-9]+(?:\.[0-9]+)?)\s+"
        r"([0-9]+(?:\.[0-9]+)?)\s+"
        r"([0-9]+(?:\.[0-9]+)?)\s+"
        r"([0-9]+(?:\.[0-9]+)?)"
    )

    for match in pattern.finditer(text):
        rows.append(BocdExchangeRate(
            currency=match.group(1),
            cash_buy=Decimal(match.group(2)),
            fx_buy=Decimal(match.group(3)),
            sell=Decimal(match.group(4)),
            middle=Decimal(match.group(5)),
            published_at=published_at,
        ))

    if not rows:
        raise ValueError("BOCD exchange rate rows not found")

    return rows


def fetch_bocd_exchange_rate_html() -> str:
    request = urllib.request.Request(
        BOCD_EXCHANGE_RATE_URL,
        headers={"User-Agent": "Mozilla/5.0 bocd-service/1.0"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        content = response.read()
        charset = response.headers.get_content_charset() or "utf-8"
        try:
            return content.decode(charset)
        except UnicodeDecodeError:
            return content.decode("gb18030")


def save_bocd_exchange_rates(session: Session, rates: List[BocdExchangeRate]) -> List[BocdExchangeRate]:
    saved_rates = []
    for rate in rates:
        statement = select(BocdExchangeRate).where(
            BocdExchangeRate.published_at == rate.published_at,
            BocdExchangeRate.currency == rate.currency,
        )
        db_rate = session.exec(statement).first()
        if db_rate:
            db_rate.cash_buy = rate.cash_buy
            db_rate.fx_buy = rate.fx_buy
            db_rate.sell = rate.sell
            db_rate.middle = rate.middle
            db_rate.fetched_at = datetime.datetime.utcnow()
            saved_rates.append(db_rate)
        else:
            session.add(rate)
            saved_rates.append(rate)

    session.commit()
    for rate in saved_rates:
        session.refresh(rate)

    return saved_rates


def fetch_and_save_bocd_exchange_rates() -> List[BocdExchangeRate]:
    html = fetch_bocd_exchange_rate_html()
    rates = parse_bocd_exchange_rates(html)
    with Session(psql_engine) as session:
        return save_bocd_exchange_rates(session, rates)


# --- OKCard purchase history --------------------------------------------

# Each service worker keeps its own tokens. The lock prevents simultaneous
# requests from logging in more than once for the same credentials.
_okcard_tokens = {}
_okcard_token_lock = threading.Lock()


class _OKCardTokenRejected(HTTPException):
    """The history API rejected the token or returned an application error."""


def _okcard_get_token(login_id: str, password: str) -> str:
    key = (login_id, password)
    with _okcard_token_lock:
        if key not in _okcard_tokens:
            _okcard_tokens[key] = okcard_login(login_id, password)
        return _okcard_tokens[key]


def _okcard_invalidate_token(login_id: str, password: str, token: str) -> None:
    key = (login_id, password)
    with _okcard_token_lock:
        # A concurrent request may already have replaced the rejected token.
        if _okcard_tokens.get(key) == token:
            del _okcard_tokens[key]


def _okcard_request(url: str, method: str = "GET", headers: Union[dict, None] = None,
                    data: Union[bytes, None] = None) -> "tuple[int, bytes]":
    request = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            # Read the error body so callers can surface the gateway's message.
            return exc.code, exc.read()
    except urllib.error.URLError:
        raise
    except (OSError, http.client.HTTPException) as exc:
        # Failures while reading a body (IncompleteRead, connection reset, SSL
        # errors, socket.timeout on Python 3.9) are network failures too.
        raise urllib.error.URLError(exc) from exc


def okcard_login(login_id: str, password: str) -> str:
    # POST JSON + User-Agent only, no app-gate basic auth here (matches the app).
    body = json.dumps({"login_id": login_id, "password": password}).encode("utf-8")
    status, raw = _okcard_request(
        OKCARD_BASE_URL + "/auth/login",
        method="POST",
        headers={"User-Agent": OKCARD_USER_AGENT, "Content-Type": "application/json"},
        data=body,
    )
    try:
        payload = json.loads(raw)
    except ValueError:
        payload = None
    token = None
    if isinstance(payload, dict) and isinstance(payload.get("response"), dict):
        token = payload["response"].get("access_token")
    if not 200 <= status < 300 or not isinstance(token, str) or not token:
        snippet = raw[:200].decode("utf-8", "replace")
        raise HTTPException(status_code=502, detail=f"OKCard login failed (HTTP {status}): {snippet}")
    return token


def okcard_fetch_history(token: str) -> List[dict]:
    # last_access_date is a lower bound; an old date returns everything, then
    # we filter to the requested month. App-gate basic auth goes here.
    basic = base64.b64encode(f"{OKCARD_APP_USER}:{OKCARD_APP_PASS}".encode()).decode()
    status, raw = _okcard_request(
        OKCARD_BASE_URL + "/store/purchase_history?last_access_date=2000-01-01%2000%3A00%3A00",
        headers={
            "User-Agent": OKCARD_USER_AGENT,
            "Authorization": f"Basic {basic}",
            "Idtoken": token,
            "Content-Type": "application/json; charset=utf-8",
        },
    )
    try:
        payload = json.loads(raw)
    except ValueError:
        payload = None
    if not 200 <= status < 300 or not isinstance(payload, dict) or payload.get("result") != "success":
        snippet = raw[:200].decode("utf-8", "replace")
        error = payload.get("error") if isinstance(payload, dict) else None
        # OKCard also rejects invalid tokens with HTTP 400 and code a001.
        # Other HTTP 400 errors must not trigger another login.
        invalid_token = (
            status == 400 and isinstance(error, dict)
            and error.get("error_code") == "a001"
        )
        # Some gateways report rejection in JSON with HTTP 200. Retry that
        # explicit application failure once, but not malformed responses,
        # network failures, rate limits, or upstream server errors.
        if invalid_token or status in (401, 403) or (
            200 <= status < 300 and isinstance(payload, dict)
            and "result" in payload and payload["result"] != "success"
        ):
            raise _OKCardTokenRejected(
                status_code=502, detail=f"OKCard history fetch failed (HTTP {status}): {snippet}")
        raise HTTPException(status_code=502, detail=f"OKCard history fetch failed (HTTP {status}): {snippet}")
    response = payload.get("response")
    if not isinstance(response, list):
        raise HTTPException(status_code=502, detail="OKCard history response must be a list")
    return response


def okcard_cached_history(login_id: str, password: str) -> List[dict]:
    for attempt in range(2):
        token = _okcard_get_token(login_id, password)
        try:
            return okcard_fetch_history(token)
        except _OKCardTokenRejected:
            _okcard_invalidate_token(login_id, password, token)
            if attempt == 1:
                raise


def _as_int(value) -> int:
    try:
        return int(Decimal(str(value)))
    except (TypeError, ValueError, ArithmeticError):
        return 0


def okcard_login_id() -> str:
    login_id = os.environ.get("OKCARD_USER")
    if not login_id:
        raise HTTPException(status_code=500, detail="OKCARD_USER not set")
    return login_id


def okcard_credentials():
    login_id = okcard_login_id()
    password = os.environ.get("OKCARD_PASS")
    if not password:
        raise HTTPException(status_code=500, detail="OKCARD_PASS not set")
    return login_id, password


def sync_okcard_history() -> dict:
    login_id, password = okcard_credentials()
    identity_field = os.environ.get("OKCARD_PURCHASE_ID_FIELD", "").strip()
    if not identity_field:
        raise HTTPException(status_code=500, detail="Set OKCARD_PURCHASE_ID_FIELD; run python okcard_sync.py --inspect first")
    try:
        return okcard_storage.sync_history(
            psql_engine, okcard_storage.account_key(login_id), identity_field,
            lambda: okcard_cached_history(login_id, password),
        )
    except okcard_storage.SyncInProgress as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except okcard_storage.SyncConfigurationError as exc:
        # A server configuration problem, like the missing-variable errors above;
        # 409 is reserved for a concurrent sync, which a retry can resolve.
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise HTTPException(status_code=502, detail="OKCard is unavailable; saved history was preserved") from exc
    except SQLAlchemyError as exc:
        raise HTTPException(status_code=503, detail="OKCard database sync failed") from exc


# Swap the data into the <script id="data"> block so okcard-history.html renders
# the fetched month. The page's own JS derives the title, totals and chart.
_OKCARD_DATA_RE = re.compile(
    r'(<script id="data" type="application/json">).*?(</script>)', re.DOTALL)


def render_okcard_html(year_month: str, purchases: List[dict]) -> str:
    try:
        with open(OKCARD_HTML_TEMPLATE, encoding="utf-8") as fh:
            template = fh.read()
    except FileNotFoundError:
        raise HTTPException(status_code=500, detail="okcard-history.html template not found")

    payload = {
        "year_month": year_month,
        "purchases": [
            {
                "store_name": p.get("store_name"),
                "purchase_price": p.get("purchase_price"),
                "purchase_date": p.get("purchase_date"),
                "items": [
                    {"item_name": it.get("item_name"),
                     "item_count": it.get("item_count"),
                     "price": it.get("price")}
                    for it in (p.get("items") or [])
                ],
            }
            for p in purchases
        ],
    }
    # Escape every "<" so a value can neither close the <script> tag early
    # ("</script>") nor switch the parser into an escaped state ("<!--<script").
    data_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c")
    # Function replacement avoids backreference handling on the JSON text.
    return _OKCARD_DATA_RE.sub(lambda m: m.group(1) + data_json + m.group(2), template, count=1)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # BOCD rates are fetched by deploy/bocd-fetch.timer, not an in-process loop.
    create_db_and_tables()
    yield
    print("shutdown event")


app = FastAPI(lifespan=lifespan)


@app.post("/fetch_bocd_exchange_rates")
async def fetch_bocd_exchange_rates() -> List[BocdExchangeRate]:
    return await asyncio.to_thread(fetch_and_save_bocd_exchange_rates)


@app.get("/bocd_exchange_rates")
def read_bocd_exchange_rates(
    session: SessionDep,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
) -> List[BocdExchangeRate]:
    rates = session.exec(
        select(BocdExchangeRate)
        .order_by(BocdExchangeRate.published_at.desc(), BocdExchangeRate.currency)
        .offset(offset)
        .limit(limit)
    ).all()
    return rates


@app.get("/bocd_exchange_rates_count")
def read_bocd_exchange_rates_count(session: SessionDep) -> int:
    rates_count = session.exec(select(func.count(BocdExchangeRate.id))).one()
    return rates_count


@app.get("/latest_jpy_sell")
def read_latest_jpy_sell(session: SessionDep):
    rate = session.exec(
        select(BocdExchangeRate)
        .where(BocdExchangeRate.currency == "日元")
        .order_by(BocdExchangeRate.published_at.desc())
        .limit(1)
    ).first()

    if not rate:
        raise HTTPException(status_code=404, detail="JPY exchange rate not found")

    return {
        "currency": rate.currency,
        "sell": rate.sell,
        "published_at": rate.published_at,
    }


@app.post("/okcard_history/sync")
def sync_okcard_history_endpoint():
    """Import all available purchases, preserving previously saved history."""
    return sync_okcard_history()


@app.get("/okcard_history/sync_status")
def read_okcard_sync_status(session: SessionDep):
    return okcard_storage.sync_status(session, okcard_storage.account_key(okcard_login_id()))


@app.get("/okcard_history")
def read_okcard_history(
    session: SessionDep,
    year_month: Annotated[str, Query(pattern=r"^\d{4}-\d{2}$")],
    fmt: Annotated[str, Query(alias="format", pattern=r"^(html|json)$")] = "html",
):
    """Read saved purchases for a month; import with POST /okcard_history/sync.

    The existing HTML and JSON response formats are preserved. Reads do not
    contact OKCard and remain available after an upstream sync failure.
    """
    try:
        month = okcard_storage.read_month(
            session, okcard_storage.account_key(okcard_login_id()), year_month)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except okcard_storage.HistoryNotSynced as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    if fmt == "json":
        total_spent = sum(_as_int(p.get("purchase_price")) for p in month)
        total_items = sum(
            _as_int(item.get("item_count"))
            for p in month
            for item in (p.get("items") or [])
        )
        return {
            "year_month": year_month,
            "purchases": month,
            "summary": {
                "purchases": len(month),
                "total_items": total_items,
                "total_spent": total_spent,
            },
        }

    return HTMLResponse(render_okcard_html(year_month, month))


if __name__ == "__main__":
    # Local development only (auto-reload); deployments run uvicorn via systemd.
    uvicorn.run("main:app", host="127.0.0.1", port=3007, reload=True)
