import hashlib
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
# Changes with the files, so a browser never keeps running last deploy's JS or CSS.
templates.env.globals["asset_version"] = hashlib.sha256(b"".join(
    open(os.path.join(HERE, "static", name), "rb").read() for name in ("app.css", "app.js")
)).hexdigest()[:10]


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


def local(path: str, fallback: str) -> str:
    """Only same-site paths, so a crafted form cannot redirect off the app."""
    return path if path.startswith("/") and not path.startswith("//") else fallback


def receipt_text(settings: dict, r: dict) -> str:
    return ledger.fill(settings["wa_receipt"], {
        "name": r["name"], "apt": r["apt_id"], "amount": ledger.money(r["amount"]),
        "period": ledger.period_he(r["period"]), "receipt_no": r["no"],
    })


def refresh() -> None:
    """Creates this month's dues and any recurring expense whose day has come; writes only when needed."""
    through = today().strftime("%Y-%m")
    with store.read() as conn:
        missing = ledger.missing_dues(conn, through) or ledger.missing_recurring(conn, today())
    if missing:
        with store.write() as conn:
            ledger.ensure_dues(conn, through)
            ledger.ensure_recurring(conn, today())


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
def grid(request: Request, year: int | None = None, month: str | None = None):
    refresh()
    current = today().strftime("%Y-%m")
    shown = ledger.check_period(month) if month else current
    year = year or int(shown[:4])
    with store.read() as conn:
        apartments = conn.execute("SELECT * FROM apartments ORDER BY id").fetchall()
        cells = ledger.grid(conn, year, current)
        shown_cells = ledger.grid(conn, int(shown[:4]), current)
        next_month = ledger.period_add(shown, 1)
        next_cells = ledger.grid(conn, int(next_month[:4]), current)
        balances = ledger.balances(conn)
    row = [shown_cells.get((a["id"], shown)) or {} for a in apartments]
    marked = ("paid", "received")
    upcoming = [next_cells.get((a["id"], next_month)) or {} for a in apartments]
    return render(
        request, "grid.html", year=year, apartments=apartments, cells=cells, shown_cells=shown_cells,
        balances=balances, periods=[f"{year}-{m:02d}" for m in range(1, 13)], current=current, shown=shown,
        prev_month=ledger.period_add(shown, -1), next_month=next_month,
        received=sum(1 for c in row if c.get("status") in marked),
        # A future month is not missing anything yet, just not marked.
        missing_apts=[a["id"] for a, c in zip(apartments, row) if c.get("status") not in marked],
        with_receipt=sum(1 for c in row if c.get("status") == "paid"),
        next_ready=sum(1 for c in upcoming if c.get("status") in marked),
        next_missing=[a["id"] for a, c in zip(apartments, upcoming) if c.get("status") not in marked],
        rows=(len(apartments) + 1) // 2,
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
    deposited: Annotated[str, Form()] = "",
    note: Annotated[str, Form()] = "",
):
    with store.write() as conn:
        no = ledger.record_payment(
            conn, apt_id=apt_id, period=period, amount=ledger.parse_money(amount), method=method,
            paid_on=parse_date(paid_on, "תאריך"), now=now(), cheque_no=cheque_no,
            deposited=bool(deposited), note=note,
        )
    return see(f"/receipts/{no}?new=1" if no else "/todo")


@app.get("/todo")
def todo(request: Request):
    refresh()
    day = today().isoformat()
    with store.read() as conn:
        held = ledger.held_payments(conn)
        settings = db.settings(conn)
        issued = [ledger.receipt(conn, r["no"]) for r in ledger.recent_receipts(conn, 50)
                  if r["issued_at"][:10] == day and not r["voided_at"]]
        debts = ledger.arrears(conn)
    sent = [{"r": r, "text": receipt_text(settings, r), "phone": ledger.wa_phone(r["phone"])} for r in issued]
    due = sum(1 for p in held if p["method"] == "cheque" and p["cheque_no"] and p["period"] <= day[:7])
    reminders = [{
        "apartment": d["apartment"], "owed": d["owed"], "phone": ledger.wa_phone(d["apartment"]["phone"]),
        "text": ledger.fill(settings["wa_reminder"], {"name": ledger.apartment_name(d["apartment"]), "apt": d["apartment"]["id"],
                                                       "balance": ledger.money(d["owed"])}),
    } for d in debts]
    return render(request, "todo.html", held=held, due=due, issued=sent, reminders=reminders, year=today().year)


@app.post("/marks")
def mark(
    apt_id: Annotated[int, Form()],
    period: Annotated[str, Form()],
    method: Annotated[str, Form()],
    back: Annotated[str, Form()] = "/grid",
):
    with store.write() as conn:
        ledger.mark(conn, apt_id, period, method, today())
    return see(local(back, "/grid"))


@app.post("/payments/{payment_id}/unmark")
def unmark(payment_id: int, back: Annotated[str, Form()] = "/grid"):
    with store.write() as conn:
        ledger.unmark(conn, payment_id)
    return see(local(back, "/grid"))


@app.post("/payments/{payment_id}/cheque")
def cheque_number(payment_id: int, cheque_no: Annotated[str, Form()], back: Annotated[str, Form()] = "/todo"):
    with store.write() as conn:
        ledger.set_cheque_no(conn, payment_id, cheque_no)
    return see(local(back, "/todo"))


@app.post("/payments/{payment_id}/clear")
def clear_payment(payment_id: int, back: Annotated[str, Form()] = "/todo"):
    with store.write() as conn:
        ledger.clear_payment(conn, payment_id, today(), now())
    return see(local(back, "/todo"))


@app.post("/payments/{payment_id}/bounce")
def bounce(payment_id: int, back: Annotated[str, Form()] = "/todo"):
    with store.write() as conn:
        ledger.bounce_cheque(conn, payment_id, now())
    return see(local(back, "/todo"))


@app.post("/deposit-due")
def deposit_due(back: Annotated[str, Form()] = "/todo"):
    with store.write() as conn:
        ledger.deposit_due(conn, today(), now())
    return see(local(back, "/todo"))


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
    return render(request, "receipt.html", r=r, s=settings, new=new, location=store.location(r["s3_key"]),
                  text=receipt_text(settings, r), phone=ledger.wa_phone(r["phone"]))


@app.get("/receipts/{no}/pdf")
def receipt_pdf(no: int):
    with store.read() as conn:
        r = ledger.receipt(conn, no)
        settings = db.settings(conn)
    # Rendered on first open, then kept: the stored file is what the resident got.
    body = store.get_file(r["s3_key"])
    if body is None:
        body = pdf.receipt_pdf(r, settings, store.location(r["s3_key"]))
        store.put_file(r["s3_key"], body, "application/pdf")
    return Response(body, media_type="application/pdf",
                    headers={"content-disposition": f'inline; filename="receipt-{no}.pdf"'})


@app.post("/receipts/{no}/void")
def void_receipt(no: int, reason: Annotated[str, Form()]):
    with store.write() as conn:
        ledger.void_receipt(conn, no, reason, now())
    return see(f"/receipts/{no}")


@app.get("/apartments")
def apartments(request: Request, saved: bool = False):
    with store.read() as conn:
        rows = conn.execute("SELECT * FROM apartments ORDER BY id").fetchall()
        opening = ledger.opening_balances(conn)
        settings = db.settings(conn)
    year = today().year
    shares = {a["id"]: ledger.fill(settings["wa_statement"], {"name": ledger.apartment_name(a), "year": year, "apt": a["id"]})
              for a in rows}
    return render(request, "apartments.html", apartments=rows, opening=opening, saved=saved, year=year,
                  shares=shares, phones={a["id"]: ledger.wa_phone(a["phone"]) for a in rows})


@app.post("/apartments")
async def save_apartments(request: Request):
    form = await request.form()
    with store.write() as conn:
        for (apt_id,) in conn.execute("SELECT id FROM apartments ORDER BY id").fetchall():
            field = lambda name: str(form.get(f"{name}_{apt_id}", ""))  # noqa: E731
            ledger.update_apartment(
                conn, apt_id, owner=field("owner"), occupant=field("occupant"), phone=field("phone"),
                monthly_fee=ledger.parse_money(field("monthly_fee")), default_method=field("default_method"),
                opening=ledger.parse_money(field("opening")),
            )
    return see("/apartments?saved=1")


@app.get("/apartments/{apt_id}/statement")
def apartment_statement(apt_id: int, year: int | None = None):
    with store.read() as conn:
        st = ledger.statement(conn, apt_id, year or today().year)
        settings = db.settings(conn)
    body = pdf.statement_pdf(st, settings, today().strftime("%d/%m/%Y"))
    return Response(body, media_type="application/pdf",
                    headers={"content-disposition": f'inline; filename="statement-{apt_id}-{st["year"]}.pdf"'})


# --- expenses -----------------------------------------------------------------

MAX_UPLOAD = 5 * 1024 * 1024


@app.get("/expenses")
def expenses_page(request: Request, year: int | None = None):
    refresh()
    year = year or today().year
    with store.read() as conn:
        rows = ledger.expenses(conn, year)
        rules = ledger.recurring_rules(conn)
        settings = db.settings(conn)
    categories = [c.strip() for c in settings["expense_categories"].splitlines() if c.strip()]
    return render(request, "expenses.html", expenses=rows, year=year, categories=categories, rules=rules,
                  total=sum(e["amount"] for e in rows), today=today().isoformat(), period=today().strftime("%Y-%m"))


@app.post("/recurring")
def add_recurring(
    category: Annotated[str, Form()],
    amount: Annotated[str, Form()],
    account: Annotated[str, Form()],
    day: Annotated[int, Form()],
    start_period: Annotated[str, Form()],
    months: Annotated[str, Form()] = "",
    vendor: Annotated[str, Form()] = "",
):
    if months.strip() and not months.strip().isdigit():
        raise Invalid("מספר חודשים: מספר שלם, או ריק ללא הגבלה")
    with store.write() as conn:
        ledger.add_recurring(conn, category=category, vendor=vendor, amount=ledger.parse_money(amount), account=account,
                             day=day, start_period=start_period, months=int(months) if months.strip() else None)
        ledger.ensure_recurring(conn, today())
    return see("/expenses#recurring")


@app.post("/recurring/{rule_id}/stop")
def stop_recurring(rule_id: int):
    with store.write() as conn:
        ledger.stop_recurring(conn, rule_id)
    return see("/expenses#recurring")


@app.post("/expenses")
async def add_expense(request: Request):
    form = await request.form()
    uploads = [f for f in form.getlist("files") if getattr(f, "filename", "")]
    files = []
    for upload in uploads:
        data = await upload.read()
        if len(data) > MAX_UPLOAD:
            raise Invalid(f"הקובץ {upload.filename} גדול מ-5MB")
        files.append((upload.filename, upload.content_type or "", data))
    with store.write() as conn:
        expense_id = ledger.add_expense(
            conn, day=parse_date(str(form.get("date", "")), "תאריך"), vendor=str(form.get("vendor", "")),
            category=str(form.get("category", "")), amount=ledger.parse_money(str(form.get("amount", ""))),
            account=str(form.get("account", "")), note=str(form.get("note", "")),
        )
        # Stored before the row commits: a failed upload leaves no expense pointing at nothing.
        for name, content_type, data in files:
            store.put_file(ledger.attach_file(conn, expense_id, name, content_type), data, content_type)
    return see(f"/expenses?year={str(form.get('date', ''))[:4]}")


@app.get("/expenses/files/{file_id}")
def expense_file(file_id: int):
    with store.read() as conn:
        f = ledger.expense_file(conn, file_id)
    body = store.get_file(f["s3_key"])
    if body is None:
        raise Invalid("הקובץ לא נמצא באחסון")
    return Response(body, media_type=f["content_type"], headers={"content-disposition": "inline"})


@app.post("/expenses/{expense_id}/delete")
def delete_expense(expense_id: int, back: Annotated[str, Form()] = "/expenses"):
    with store.write() as conn:
        ledger.delete_expense(conn, expense_id)
    return see(local(back, "/expenses"))


# --- the two accounts and the yearly summary ----------------------------------

@app.get("/accounts")
def accounts(request: Request, saved: bool = False):
    refresh()
    with store.read() as conn:
        balances = ledger.account_balances(conn)
        recon = ledger.reconciliation(conn)
        transfers = conn.execute("SELECT * FROM transfers ORDER BY date DESC, id DESC LIMIT 20").fetchall()
        incomes = conn.execute("SELECT * FROM incomes ORDER BY date DESC, id DESC LIMIT 20").fetchall()
        statements = conn.execute("SELECT * FROM bank_statements ORDER BY date DESC LIMIT 6").fetchall()
        settings = db.settings(conn)
        years = [r[0] for r in conn.execute(
            "SELECT DISTINCT substr(period, 1, 4) FROM payments UNION SELECT DISTINCT substr(date, 1, 4) FROM expenses "
            "ORDER BY 1 DESC")]
    year = today().year
    return render(request, "accounts.html", balances=balances, recon=recon, transfers=transfers, incomes=incomes,
                  statements=statements,
                  s=settings, saved=saved, today=today().isoformat(), years=years or [str(year)],
                  summary_text={y: ledger.fill(settings["wa_summary"], {"year": y}) for y in (years or [str(year)])})


@app.post("/accounts/openings")
def save_openings(
    accounts_start: Annotated[str, Form()],
    opening_bank: Annotated[str, Form()],
    opening_treasurer: Annotated[str, Form()],
):
    with store.write() as conn:
        db.save_settings(conn, {
            "accounts_start": parse_date(accounts_start, "תאריך פתיחה").isoformat(),
            "opening_bank": str(ledger.parse_money(opening_bank)),
            "opening_treasurer": str(ledger.parse_money(opening_treasurer)),
        })
    return see("/accounts?saved=1")


@app.post("/accounts/statement")
def bank_statement(day: Annotated[str, Form()], balance: Annotated[str, Form()]):
    with store.write() as conn:
        ledger.record_bank_statement(conn, parse_date(day, "תאריך"), ledger.parse_money(balance))
    return see("/accounts")


@app.post("/transfers")
def add_transfer(
    day: Annotated[str, Form()],
    from_account: Annotated[str, Form()],
    amount: Annotated[str, Form()],
    cheque_no: Annotated[str, Form()] = "",
    note: Annotated[str, Form()] = "",
):
    with store.write() as conn:
        ledger.add_transfer(conn, day=parse_date(day, "תאריך"), from_account=from_account,
                            amount=ledger.parse_money(amount), cheque_no=cheque_no, note=note)
    return see("/accounts")


@app.post("/incomes")
def add_income(
    day: Annotated[str, Form()],
    description: Annotated[str, Form()],
    amount: Annotated[str, Form()],
    account: Annotated[str, Form()],
    note: Annotated[str, Form()] = "",
):
    with store.write() as conn:
        ledger.add_income(conn, day=parse_date(day, "תאריך"), description=description,
                          amount=ledger.parse_money(amount), account=account, note=note)
    return see("/accounts#incomes")


@app.post("/incomes/{income_id}/delete")
def delete_income(income_id: int):
    with store.write() as conn:
        conn.execute("DELETE FROM incomes WHERE id = ?", (income_id,))
    return see("/accounts#incomes")


@app.post("/transfers/{transfer_id}/delete")
def delete_transfer(transfer_id: int):
    with store.write() as conn:
        conn.execute("DELETE FROM transfers WHERE id = ?", (transfer_id,))
    return see("/accounts")


@app.get("/summary/{year}/pdf")
def summary_pdf(year: int):
    refresh()
    with store.read() as conn:
        sm = ledger.year_summary(conn, year)
        settings = db.settings(conn)
    key = f"summaries/{year}.pdf"
    # Regenerated each time; bucket versioning keeps every copy that was sent out.
    body = pdf.summary_pdf(sm, settings, today().strftime("%d/%m/%Y"), store.location(key))
    store.put_file(key, body, "application/pdf")
    return Response(body, media_type="application/pdf",
                    headers={"content-disposition": f'inline; filename="summary-{year}.pdf"'})


@app.get("/more")
def more(request: Request):
    return render(request, "more.html")


@app.get("/settings")
def settings_page(request: Request, saved: bool = False):
    with store.read() as conn:
        values = db.settings(conn)
    return render(request, "settings.html", s=values, saved=saved)


@app.post("/settings")
def save_settings(
    building_name: Annotated[str, Form()],
    start_period: Annotated[str, Form()],
    wa_receipt: Annotated[str, Form()],
    wa_statement: Annotated[str, Form()],
    wa_reminder: Annotated[str, Form()],
    wa_summary: Annotated[str, Form()],
    expense_categories: Annotated[str, Form()],
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
            "wa_statement": wa_statement.strip(),
            "wa_reminder": wa_reminder.strip(),
            "wa_summary": wa_summary.strip(),
            "expense_categories": "\n".join(c.strip() for c in expense_categories.splitlines() if c.strip()),
        })
    return see("/settings?saved=1")


handler = Mangum(app, lifespan="off")
