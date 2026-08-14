"""
Meridian Credit Union - Member Servicing (MOCK TARGET APP)

A deliberately "legacy" server-rendered app used as the proxy target for the
computer-use automation system. No API, no test IDs, table-based layout,
inline JS confirm() dialogs, and a handful of runtime conditions
(validation errors, permission denials, transient failures) that a real
back-office banking screen would exhibit.

This is NOT part of the automation system itself - it's the thing being
automated. Run with: python target_app/app.py
"""
import time
from flask import Flask, request, redirect, session, url_for, render_template

app = Flask(__name__)
app.secret_key = "dev-only-not-a-real-secret"

# ---- in-memory "core banking" data ----------------------------------------
MEMBERS = {
    "12345": {"name": "Alicia Chen", "savings": 4820.55, "checking": 1210.10, "status": "active"},
    "67890": {"name": "Robert Duarte", "savings": 150.00, "checking": 0.00, "status": "active"},
    "55555": {"name": "Transient Tester", "savings": 999.00, "checking": 10.00, "status": "active"},
    "00042": {"name": "Restricted Holdings LLC", "savings": 55000.00, "checking": 2000.00, "status": "restricted"},
}
RESTRICTED_PREFIX = "000"  # member ids starting with this => permission denied
NEXT_SUBACCOUNT_NO = [90001]

# per-process counters to simulate transient failures (first attempt fails, retry succeeds)
_transient_attempts = {}


def _require_login():
    return session.get("user") == "operator"


@app.route("/health")
def health():
    return {"status": "ok"}


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        u = request.form.get("username", "")
        p = request.form.get("password", "")
        if u == "operator" and p == "demo123":
            session["user"] = "operator"
            return redirect(url_for("search"))
        error = "Invalid username or password."
    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/search", methods=["GET", "POST"])
def search():
    if not _require_login():
        return redirect(url_for("login"))
    error = None
    if request.method == "POST":
        mid = request.form.get("member_id", "").strip()
        if mid in MEMBERS:
            return redirect(url_for("member_detail", member_id=mid))
        error = f"No member found matching ID '{mid}'."
    return render_template("search.html", error=error)


@app.route("/member/<member_id>")
def member_detail(member_id):
    if not _require_login():
        return redirect(url_for("login"))
    m = MEMBERS.get(member_id)
    if not m:
        return render_template("search.html", error=f"No member found matching ID '{member_id}'."), 404
    return render_template("member.html", member_id=member_id, m=m)


@app.route("/member/<member_id>/new-subaccount", methods=["GET", "POST"])
def new_subaccount(member_id):
    if not _require_login():
        return redirect(url_for("login"))

    m = MEMBERS.get(member_id)
    if not m:
        return render_template("search.html", error=f"No member found matching ID '{member_id}'."), 404

    if member_id.startswith(RESTRICTED_PREFIX):
        return render_template(
            "member_form.html", member_id=member_id, m=m,
            permission_denied=True,
        )

    error = None
    if request.method == "POST":
        acct_type = request.form.get("account_type", "")
        deposit_raw = request.form.get("initial_deposit", "")

        # simulate one transient "system busy" failure per member, then succeed on retry
        attempts = _transient_attempts.get(member_id, 0)
        if member_id == "55555" and attempts == 0:
            _transient_attempts[member_id] = attempts + 1
            return render_template(
                "member_form.html", member_id=member_id, m=m,
                transient_error=True,
            )

        try:
            deposit = float(deposit_raw)
        except ValueError:
            deposit = None

        if acct_type not in ("share", "money_market", "certificate"):
            error = "Please select a valid account type."
        elif deposit is None or deposit < 25:
            error = "Initial deposit must be a number of at least $25.00."

        if error:
            return render_template(
                "member_form.html", member_id=member_id, m=m, error=error,
            )

        # success
        acct_no = NEXT_SUBACCOUNT_NO[0]
        NEXT_SUBACCOUNT_NO[0] += 1
        return render_template(
            "confirmation.html", member_id=member_id, m=m,
            acct_no=acct_no, acct_type=acct_type, deposit=deposit,
        )

    return render_template("member_form.html", member_id=member_id, m=m)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5055, debug=False)
