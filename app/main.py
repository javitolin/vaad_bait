import os
from datetime import date, datetime
from typing import Annotated
from urllib.parse import quote
from zoneinfo import ZoneInfo

import boto3
from fastapi import FastAPI, Form, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from mangum import Mangum

import auth
import db
import ledger
import pdf
from ledger import Invalid
from store import Conflict, Store

HERE = os.path.dirname(os.path.abspath(__file__))
TZ = ZoneInfo("Asia/Jerusalem")

# Set only by `make dev`. Refused on Lambda so a stray variable can never
# switch login off in production.
DEV_USER = os.environ.get("DEV_USER", "")
if DEV_USER and os.environ.get("AWS_LAMBDA_FUNCTION_NAME"):
    raise RuntimeError("DEV_USER must not be set on Lambda")

BUCKET = os.environ.get("BUCKET")
store = Store(os.environ.get("DATA_DIR", "/tmp/vaad"), boto3.client("s3") if BUCKET else None, BUCKET)

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=os.path.join(HERE, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(HERE, "templates"))
templates.env.filters.update(money=ledger.money, period_he=ledger.period_he, date_he=ledger.date_he,
                             shekels=lambda agorot: f"{round(agorot / 100):,}")
templates.env.globals.update(METHOD_HE=ledger.METHOD_HE, METHOD_SHORT=ledger.METHOD_SHORT, MONTHS_HE=ledger.MONTHS_HE)


def now() -> datetime:
    return datetime.now(TZ)


def today() -> date:
    return now().date()


def parse_date(text: str, label: str) -> date:
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise Invalid(f"{label}: תאריך לא תקין") from None


def render(request: Request, name: str, status: int = 200, **context) -> Response:
    return templates.TemplateResponse(request, name, context, status_code=status)


def see(path: str) -> RedirectResponse:
    return RedirectResponse(path, status_code=303)


def refresh_dues() -> None:
    through = today().strftime("%Y-%m")
    with store.read() as conn:
        missing = ledger.missing_dues(conn, through)
    if missing:
        with store.write() as conn:
            ledger.ensure_dues(conn, through)


# --- auth ---------------------------------------------------------------------

@app.middleware("http")
async def require_session(request: Request, call_next):
    if request.url.path.startswith(("/auth/", "/static/")):
        return await call_next(request)
    user = DEV_USER or auth.session_user(request.cookies.get(auth.SESSION_COOKIE))
    if not user:
        if request.method != "GET":
            # A fetch() cannot follow a redirect to Cognito; app.js sends the page there.
            return Response(status_code=401)
        target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
        return RedirectResponse("/auth/login?next=" + quote(target), status_code=302)
    request.state.user = user
    return await call_next(request)


@app.get("/auth/login")
def login(next: str = "/"):
    return RedirectResponse(next if next.startswith("/") else "/", 302) if DEV_USER else auth.login(next)


@app.get("/auth/callback")
def callback(request: Request):
    return auth.callback(dict(request.query_params), request.cookies)


@app.get("/auth/logout")
def logout():
    return RedirectResponse("/", 302) if DEV_USER else auth.logout()


@app.exception_handler(Invalid)
def invalid(request: Request, exc: Invalid):
    return render(request, "error.html", 400, message=str(exc))


@app.exception_handler(RequestValidationError)
def malformed(request: Request, exc: RequestValidationError):
    fields = ", ".join(str(e["loc"][-1]) for e in exc.errors())
    return render(request, "error.html", 400, message=f"שדות חסרים או לא תקינים: {fields}")


@app.exception_handler(Conflict)
def conflict(request: Request, _exc: Conflict):
    return render(request, "error.html", 409, message="שמירה אחרת התבצעה באותו רגע. רענן את הדף ונסה שוב.")


# --- pages --------------------------------------------------------------------

@app.get("/")
def home():
    return RedirectResponse("/grid", 302)


@app.get("/grid")
def grid(request: Request, year: int | None = None):
    refresh_dues()
    current = today().strftime("%Y-%m")
    year = year or today().year
    with store.read() as conn:
        apartments = conn.execute("SELECT * FROM apartments ORDER BY id").fetchall()
        cells = ledger.grid(conn, year, current)
        balances = ledger.balances(conn)
        this_month = ledger.grid(conn, int(current[:4]), current)
        held = ledger.held_cheques(conn)
    month = [this_month.get((a["id"], current)) for a in apartments]
    owed = [b for b in balances.values() if b > 0]
    return render(
        request, "grid.html", year=year, apartments=apartments, cells=cells, balances=balances,
        periods=[f"{year}-{m:02d}" for m in range(1, 13)], current=current,
        month_he=ledger.MONTHS_HE[int(current[5:]) - 1],
        paid_count=sum(1 for c in month if c and c["status"] == "paid"),
        collected=sum(c["paid"] for c in month if c),
        owed_total=sum(owed), owed_count=len(owed),
        held_count=len(held), held_total=sum(c["amount"] for c in held),
        due=sum(1 for c in held if c["cheque_due"] and c["cheque_due"] <= today().isoformat()),
    )


@app.get("/payments/new")
def new_payment(request: Request, apt: int = 1, period: str = ""):
    with store.read() as conn:
        apartments = conn.execute("SELECT * FROM apartments ORDER BY id").fetchall()
    chosen = next((a for a in apartments if a["id"] == apt), apartments[0])
    return render(request, "payment.html", apartments=apartments, apt=chosen,
                  period=period or today().strftime("%Y-%m"), today=today().isoformat())


@app.post("/payments")
def create_payment(
    apt_id: Annotated[int, Form()],
    period: Annotated[str, Form()],
    amount: Annotated[str, Form()],
    method: Annotated[str, Form()],
    paid_on: Annotated[str, Form()],
    cheque_no: Annotated[str, Form()] = "",
    cheque_due: Annotated[str, Form()] = "",
    deposited: Annotated[str, Form()] = "",
    note: Annotated[str, Form()] = "",
):
    with store.write() as conn:
        no = ledger.record_payment(
            conn, apt_id=apt_id, period=period, amount=ledger.parse_money(amount), method=method,
            paid_on=parse_date(paid_on, "תאריך"), now=now(), cheque_no=cheque_no,
            cheque_due=parse_date(cheque_due, "תאריך פירעון") if cheque_due else None,
            deposited=bool(deposited), note=note,
        )
    return see(f"/receipts/{no}?new=1" if no else "/cheques")


@app.get("/cheques")
def cheques(request: Request):
    with store.read() as conn:
        held = ledger.held_cheques(conn)
        apartments = conn.execute("SELECT * FROM apartments ORDER BY id").fetchall()
    due = sum(1 for c in held if c["cheque_due"] and c["cheque_due"] <= today().isoformat())
    return render(request, "cheques.html", held=held, due=due, apartments=apartments,
                  today=today().isoformat(), period=today().strftime("%Y-%m"))


@app.post("/cheques/series")
def cheque_series(
    apt_id: Annotated[int, Form()],
    first_no: Annotated[int, Form()],
    count: Annotated[int, Form()],
    amount: Annotated[str, Form()],
    first_due: Annotated[str, Form()],
    first_period: Annotated[str, Form()],
):
    with store.write() as conn:
        ledger.add_cheque_series(
            conn, apt_id=apt_id, first_no=first_no, count=count, amount=ledger.parse_money(amount),
            first_due=parse_date(first_due, "פירעון ראשון"), first_period=first_period, received_on=today(),
        )
    return see("/cheques")


@app.post("/cheques/deposit-due")
def deposit_due():
    with store.write() as conn:
        ledger.deposit_due(conn, today(), now())
    return see("/receipts")


@app.post("/cheques/{payment_id}/clear")
def clear_cheque(payment_id: int):
    with store.write() as conn:
        no = ledger.clear_cheque(conn, payment_id, today(), now())
    return see(f"/receipts/{no}?new=1")


@app.post("/cheques/{payment_id}/bounce")
def bounce_cheque(payment_id: int):
    with store.write() as conn:
        ledger.bounce_cheque(conn, payment_id, now())
    return see("/cheques")


@app.get("/receipts")
def receipts(request: Request):
    with store.read() as conn:
        rows = ledger.recent_receipts(conn)
    return render(request, "receipts.html", receipts=rows)


@app.get("/receipts/{no}")
def receipt(request: Request, no: int, new: bool = False):
    with store.read() as conn:
        r = ledger.receipt(conn, no)
        settings = db.settings(conn)
    text = ledger.fill(settings["wa_receipt"], {
        "name": r["payer"], "apt": r["apt_id"], "amount": ledger.money(r["amount"]),
        "period": ledger.period_he(r["period"]), "receipt_no": r["no"],
    })
    return render(request, "receipt.html", r=r, s=settings, new=new, text=text, phone=ledger.wa_phone(r["phone"]))


@app.get("/receipts/{no}/pdf")
def receipt_pdf(no: int):
    with store.read() as conn:
        r = ledger.receipt(conn, no)
        settings = db.settings(conn)
    # Rendered on first open, then kept: the stored file is what the resident got.
    body = store.get_file(r["s3_key"])
    if body is None:
        body = pdf.receipt_pdf(r, settings)
        store.put_file(r["s3_key"], body, "application/pdf")
    return Response(body, media_type="application/pdf",
                    headers={"content-disposition": f'inline; filename="receipt-{no}.pdf"'})


@app.post("/receipts/{no}/void")
def void_receipt(no: int, reason: Annotated[str, Form()]):
    with store.write() as conn:
        ledger.void_receipt(conn, no, reason, now())
    return see(f"/receipts/{no}")


@app.get("/apartments")
def apartments(request: Request):
    with store.read() as conn:
        rows = conn.execute("SELECT * FROM apartments ORDER BY id").fetchall()
        opening = ledger.opening_balances(conn)
    return render(request, "apartments.html", apartments=rows, opening=opening)


@app.post("/apartments/{apt_id}")
def update_apartment(
    apt_id: int,
    monthly_fee: Annotated[str, Form()],
    default_method: Annotated[str, Form()],
    owner: Annotated[str, Form()] = "",
    occupant: Annotated[str, Form()] = "",
    phone: Annotated[str, Form()] = "",
    opening: Annotated[str, Form()] = "",
):
    with store.write() as conn:
        ledger.update_apartment(
            conn, apt_id, owner=owner, occupant=occupant, phone=phone,
            monthly_fee=ledger.parse_money(monthly_fee), default_method=default_method,
            opening=ledger.parse_money(opening),
        )
    return see(f"/apartments#apt-{apt_id}")


@app.get("/settings")
def settings_page(request: Request):
    with store.read() as conn:
        values = db.settings(conn)
    return render(request, "settings.html", s=values)


@app.post("/settings")
def save_settings(
    building_name: Annotated[str, Form()],
    start_period: Annotated[str, Form()],
    wa_receipt: Annotated[str, Form()],
    building_address: Annotated[str, Form()] = "",
    treasurer_name: Annotated[str, Form()] = "",
):
    with store.write() as conn:
        db.save_settings(conn, {
            "building_name": building_name.strip(),
            "building_address": building_address.strip(),
            "treasurer_name": treasurer_name.strip(),
            "start_period": ledger.check_period(start_period),
            "wa_receipt": wa_receipt.strip(),
        })
    return see("/settings")


handler = Mangum(app, lifespan="off")
