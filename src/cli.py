"""Command line for the triage agent.

  python -m src.cli check                      is Ollama reachable, which models exist
  python -m src.cli eval                       run all test emails, print accuracy (no side effects)
  python -m src.cli run [--ids e01,e08]        process test emails for real (writes state + outbox)
  python -m src.cli run --file path.eml|.json  process one email file
  python -m src.cli watch [--interval 5]       always-on: process files dropped in data/inbox/
  python -m src.cli outbox                     list pending messages
  python -m src.cli approve M001|all           approve (simulated send; confirms linked order)
  python -m src.cli reject M001
  python -m src.cli status                     one-screen summary of today's activity
  python -m src.cli reset                      wipe state, log, outbox
Add --json to run/approve/status for machine-readable output (handy for an agent to relay).
"""
import argparse
import collections
import email as emaillib
import email.policy
import glob
import json
import os
import shutil
import sys
import time

from . import llm
from .store import DATA, LOG_FILE, OUT, SENT_FILE, State
from .triage import CATEGORIES, process


def load_emails():
    with open(os.path.join(DATA, "emails.json")) as f:
        return json.load(f)


def load_file(path):
    if path.endswith(".json"):
        with open(path) as f:
            d = json.load(f)
        d.setdefault("id", os.path.basename(path).rsplit(".", 1)[0])
        return d
    with open(path, "rb") as f:
        m = emaillib.message_from_binary_file(f, policy=email.policy.default)
    body = m.get_body(preferencelist=("plain",))
    return {"id": os.path.basename(path).rsplit(".", 1)[0], "from": str(m["from"] or ""),
            "subject": str(m["subject"] or ""), "body": body.get_content() if body else ""}


def one_line(r):
    extra = r.get("order_id") or r.get("owner") or ""
    return "%-4s %-19s %-15s %-9s %s %s" % (
        r["email_id"], r["category"], r["decision"], ("%.1fs" % r["seconds"]), extra,
        ("| " + "; ".join(r["reasons"])) if r["reasons"] else "")


def cmd_check(a):
    print("Ollama:", llm.HOST, "| model:", llm.MODEL, "| mock:", llm.MOCK)
    try:
        names = llm.ping()
        print("models:", ", ".join(names) or "(none)")
        if llm.MODEL not in names and not any(n.startswith(llm.MODEL.split(":")[0]) for n in names):
            print("WARNING: TRIAGE_MODEL not found locally. Run: ollama pull", llm.MODEL)
        t = time.time()
        print("test call:", llm.chat("Reply with the single word OK.", "ping"), "(%.1fs)" % (time.time() - t))
    except Exception as e:
        print("FAILED to reach Ollama:", e)
        sys.exit(1)


def cmd_eval(a):
    S = State(persist=False)
    rows, tot = [], collections.Counter()
    per = collections.defaultdict(lambda: [0, 0])
    for em in load_emails():
        try:
            r = process(em, S)
        except Exception as e:  # keep going; count as a miss
            r = {"email_id": em["id"], "category": "ERROR", "decision": "ERROR", "reasons": [str(e)],
                 "seconds": 0, "flags": []}
        cat_ok = r["category"] == em["expected"]
        dec_ok = r["decision"] == em["expected_decision"]
        tot["n"] += 1
        tot["cat"] += cat_ok
        tot["dec"] += dec_ok
        tot["sec"] += r["seconds"]
        per[em["expected"]][0] += dec_ok
        per[em["expected"]][1] += 1
        rows.append((em, r, cat_ok, dec_ok))
        print("%s %s  cat=%-19s want=%-19s | dec=%-14s want=%-14s %s" % (
            "OK  " if dec_ok else "MISS", em["id"], r["category"], em["expected"], r["decision"],
            em["expected_decision"], "" if dec_ok else "<- " + "; ".join(r["reasons"])[:90]))
    n = tot["n"]
    print("\nCategory accuracy : %d/%d (%.0f%%)" % (tot["cat"], n, 100 * tot["cat"] / n))
    print("Decision accuracy : %d/%d (%.0f%%)  <- end-to-end: did the right thing happen" % (
        tot["dec"], n, 100 * tot["dec"] / n))
    print("Avg seconds/email : %.1f" % (tot["sec"] / n))
    for c in CATEGORIES:
        print("  %-19s %d/%d" % (c, per[c][0], per[c][1]))
    sec = [r for _, r, _, _ in rows if r.get("flags")]
    print("Security-flagged  : %d emails quarantined by rules" % len(sec))
    with open(os.path.join(OUT, "eval.json"), "w") as f:
        json.dump({"n": n, "category_correct": tot["cat"], "decision_correct": tot["dec"],
                   "avg_seconds": tot["sec"] / n, "model": llm.MODEL, "mock": llm.MOCK}, f, indent=2)


def cmd_run(a):
    S = State()
    if a.file:
        emails = [load_file(a.file)]
    else:
        emails = load_emails()
        if a.ids:
            keep = set(a.ids.split(","))
            emails = [e for e in emails if e["id"] in keep]
    results = [process(e, S) for e in emails]
    if a.json:
        print(json.dumps(results, indent=2))
    else:
        for r in results:
            print(one_line(r))
        print("\n%d processed. Pending messages: %d  (python -m src.cli outbox)" % (
            len(results), sum(1 for m in S.outbox if m["status"] == "pending")))


def cmd_watch(a):
    S = State()
    inbox = os.path.join(DATA, "inbox")
    done = os.path.join(inbox, "processed")
    os.makedirs(done, exist_ok=True)
    print("Watching %s every %ss. Drop .eml or .json emails there. Ctrl+C to stop." % (inbox, a.interval))
    while True:
        files = sorted(glob.glob(os.path.join(inbox, "*.eml")) + glob.glob(os.path.join(inbox, "*.json")))
        for p in files:
            try:
                r = process(load_file(p), S)
                print(one_line(r), flush=True)
            except Exception as e:
                print("ERROR on %s: %s" % (p, e), flush=True)
            shutil.move(p, os.path.join(done, os.path.basename(p)))
        time.sleep(a.interval)


def cmd_drop(a):
    inbox = os.path.join(DATA, "inbox")
    os.makedirs(inbox, exist_ok=True)
    by = {e["id"]: e for e in load_emails()}
    for i in a.ids:
        e = by[i]
        with open(os.path.join(inbox, i + ".json"), "w") as f:
            json.dump({"from": e["from"], "subject": e["subject"], "body": e["body"]}, f)
        print("dropped", i, "-", e["subject"])


def cmd_outbox(a):
    S = State()
    items = [m for m in S.outbox if a.all or m["status"] == "pending"]
    for m in items:
        print("-" * 70)
        print("%s [%s] %s -> %s  (%s)%s" % (m["id"], m["kind"], m["email_id"], m["to"], m["status"],
                                           "  order " + m["order_id"] if m["order_id"] else ""))
        print("Subject:", m["subject"])
        print(m["body"])
    print("-" * 70 + "\n%d message(s)" % len(items))


def do_approve(S, m):
    msg = "sent (simulated)"
    if m.get("order_id"):
        ok, why = S.confirm_order(m["order_id"])
        if not ok:
            m["status"] = "blocked"
            return "BLOCKED %s: %s" % (m["id"], why)
        msg += ", order %s %s" % (m["order_id"], why)
    m["status"] = "sent_simulated"
    with open(SENT_FILE, "a") as f:
        f.write(json.dumps({"ts": time.time(), "id": m["id"], "to": m["to"], "subject": m["subject"]}) + "\n")
    return "%s %s" % (m["id"], msg)


def cmd_approve(a):
    S = State()
    out = []
    for m in S.outbox:
        if m["status"] == "pending" and (a.id == "all" or m["id"] == a.id):
            out.append(do_approve(S, m))
    S.save()
    print(json.dumps(out) if a.json else "\n".join(out) or "nothing to approve")


def cmd_reject(a):
    S = State()
    for m in S.outbox:
        if m["id"] == a.id and m["status"] == "pending":
            m["status"] = "rejected"
            print("rejected", a.id)
    S.save()


def cmd_status(a):
    S = State()
    rows = []
    if os.path.exists(LOG_FILE):
        with open(LOG_FILE) as f:
            rows = [json.loads(x) for x in f if x.strip()]
    by = collections.Counter(r["decision"] for r in rows)
    pend = [m["id"] for m in S.outbox if m["status"] == "pending"]
    summary = {"emails_processed": len(rows), "decisions": dict(by),
               "pending_messages": pend,
               "avg_seconds": round(sum(r["seconds"] for r in rows) / len(rows), 1) if rows else 0}
    if a.json:
        print(json.dumps(summary))
    else:
        print("Processed %d emails | %s" % (len(rows), dict(by)))
        print("Pending approvals:", ", ".join(pend) or "none")
        if os.path.exists(os.path.join(OUT, "notifications.log")):
            print("\nRecent alerts:")
            with open(os.path.join(OUT, "notifications.log")) as f:
                print("".join(f.readlines()[-8:]))


def cmd_reset(a):
    for p in ("state.json", "log.jsonl", "sent.log", "notifications.log", "eval.json"):
        q = os.path.join(OUT, p)
        if os.path.exists(q):
            os.remove(q)
    print("state cleared")


def main():
    p = argparse.ArgumentParser(description="Bayline email triage agent")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check").set_defaults(f=cmd_check)
    sub.add_parser("eval").set_defaults(f=cmd_eval)
    r = sub.add_parser("run"); r.add_argument("--ids"); r.add_argument("--file"); r.add_argument("--json", action="store_true"); r.set_defaults(f=cmd_run)
    w = sub.add_parser("watch"); w.add_argument("--interval", type=int, default=5); w.set_defaults(f=cmd_watch)
    o = sub.add_parser("outbox"); o.add_argument("--all", action="store_true"); o.set_defaults(f=cmd_outbox)
    ap = sub.add_parser("approve"); ap.add_argument("id"); ap.add_argument("--json", action="store_true"); ap.set_defaults(f=cmd_approve)
    rj = sub.add_parser("reject"); rj.add_argument("id"); rj.set_defaults(f=cmd_reject)
    st = sub.add_parser("status"); st.add_argument("--json", action="store_true"); st.set_defaults(f=cmd_status)
    sub.add_parser("reset").set_defaults(f=cmd_reset)
    dp = sub.add_parser("drop"); dp.add_argument("ids", nargs="+"); dp.set_defaults(f=cmd_drop)
    a = p.parse_args()
    os.makedirs(OUT, exist_ok=True)
    a.f(a)


if __name__ == "__main__":
    main()
