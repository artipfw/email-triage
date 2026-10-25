"""Local data + state: products, accounts, orders, outbox. All JSON/CSV on disk, no network."""
import csv
import json
import os
import re
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
OUT = os.path.join(DATA, "out")
STATE_FILE = os.path.join(OUT, "state.json")
LOG_FILE = os.path.join(OUT, "log.jsonl")
SENT_FILE = os.path.join(OUT, "sent.log")
NOTIFY_FILE = os.path.join(OUT, "notifications.log")


def read_csv(name):
    with open(os.path.join(DATA, name), newline="") as f:
        return list(csv.DictReader(f))


def norm(s):
    return re.sub(r"[^A-Z0-9]", "", (s or "").upper())


def stem(w):
    if w.endswith("es") and len(w) > 4:
        return w[:-2]
    if w.endswith("s") and len(w) > 3:
        return w[:-1]
    return w


def tokens(text):
    return {stem(t) for t in re.findall(r"[a-z0-9]+", (text or "").lower())}


class State:
    def __init__(self, persist=True):
        self.persist = persist
        os.makedirs(OUT, exist_ok=True)
        self.products = {
            r["sku"]: {**r, "unit_price": float(r["unit_price"]), "stock": int(r["stock"])}
            for r in read_csv("products.csv")
        }
        self.accounts = {r["domain"].lower(): r for r in read_csv("accounts.csv")}
        with open(os.path.join(DATA, "orders_seed.json")) as f:
            self.orders = {o["order_id"]: o for o in json.load(f)}
        self.outbox = []
        self.counter = 2000
        if persist and os.path.exists(STATE_FILE):
            with open(STATE_FILE) as f:
                s = json.load(f)
            for sku, n in s.get("stock", {}).items():
                if sku in self.products:
                    self.products[sku]["stock"] = n
            for o in s.get("orders", []):
                self.orders[o["order_id"]] = o
            self.outbox = s.get("outbox", [])
            self.counter = s.get("counter", 2000)

    # ---- persistence -------------------------------------------------
    def save(self):
        if not self.persist:
            return
        s = {
            "stock": {k: v["stock"] for k, v in self.products.items()},
            "orders": [o for o in self.orders.values() if o.get("created_by_agent")],
            "outbox": self.outbox,
            "counter": self.counter,
        }
        with open(STATE_FILE, "w") as f:
            json.dump(s, f, indent=2)

    def log(self, entry):
        if self.persist:
            with open(LOG_FILE, "a") as f:
                f.write(json.dumps(entry) + "\n")

    def notify(self, text):
        line = "[%s] %s" % (time.strftime("%H:%M:%S"), text)
        if self.persist:
            with open(NOTIFY_FILE, "a") as f:
                f.write(line + "\n")
        return line

    # ---- products ----------------------------------------------------
    def resolve(self, sku, desc):
        """Return (product, how). how is exact|fuzzy|ambiguous|none."""
        s = (sku or "").strip().upper()
        by_upper = {k.upper(): v for k, v in self.products.items()}
        if s in by_upper:
            return by_upper[s], "exact"
        q = tokens(desc or sku)
        if not q:
            return None, "none"
        scored = []
        for p in self.products.values():
            n = tokens(p["name"]) | tokens(p["sku"])
            scored.append((len(q & n) / len(q), p))
        scored.sort(key=lambda x: -x[0])
        best = scored[0]
        if best[0] < 0.75:
            return None, "none"
        if len(scored) > 1 and scored[1][0] == best[0]:
            return None, "ambiguous"
        return best[1], "fuzzy"

    # ---- orders ------------------------------------------------------
    def find_order(self, ref):
        r = norm(ref)
        if not r:
            return None
        for o in self.orders.values():
            if r in (norm(o["order_id"]), norm(o.get("customer_po", ""))):
                return o
        return None

    def next_order_id(self):
        self.counter += 1
        return "SO-%d" % self.counter

    def create_order(self, domain, po, lines, ship_to, requested_date, total, status):
        o = {
            "order_id": self.next_order_id(),
            "customer_po": po,
            "customer_domain": domain,
            "status": status,
            "lines": lines,
            "ship_to": ship_to,
            "requested_date": requested_date,
            "total": round(total, 2),
            "created_by_agent": True,
        }
        self.orders[o["order_id"]] = o
        if status == "confirmed":
            self.reserve(o)
        return o

    def reserve(self, order):
        for ln in order["lines"]:
            self.products[ln["sku"]]["stock"] -= ln["qty"]

    def confirm_order(self, order_id):
        o = self.orders[order_id]
        if o["status"] == "confirmed":
            return True, "already confirmed"
        for ln in o["lines"]:
            if self.products[ln["sku"]]["stock"] < ln["qty"]:
                return False, "stock changed: %s only %d left" % (
                    ln["sku"], self.products[ln["sku"]]["stock"])
        o["status"] = "confirmed"
        self.reserve(o)
        return True, "confirmed"

    # ---- outbox ------------------------------------------------------
    def queue(self, email_id, to, subject, body, kind, order_id=None):
        item = {
            "id": "M%03d" % (len(self.outbox) + 1),
            "email_id": email_id,
            "to": to,
            "subject": subject,
            "body": body,
            "kind": kind,  # customer_reply | internal_note | alert
            "order_id": order_id,
            "status": "pending",
        }
        self.outbox.append(item)
        return item["id"]
