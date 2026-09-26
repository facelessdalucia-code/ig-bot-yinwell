import os
import re
import json
import html
import logging
import random
import threading
import time

import requests
from flask import Flask, request, jsonify, Response

try:
    import psycopg
except ImportError:
    psycopg = None

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("yinwell-bot")

app = Flask(__name__)

VERIFY_TOKEN = os.environ["VERIFY_TOKEN"]
IG_TOKEN = os.environ["IG_TOKEN"]
IG_USER_ID = os.environ["IG_USER_ID"]
IG_USERNAME = os.environ.get("IG_USERNAME", "yinwellhealth")
FB_PAGE_TOKEN = os.environ.get("FB_PAGE_TOKEN")
FB_PAGE_ID = os.environ.get("FB_PAGE_ID")
DM_LINK = os.environ["DM_LINK"]
DM_LINK_A = os.environ.get("DM_LINK_A") or DM_LINK.rstrip("/") + "/a/"
DM_LINK_B = os.environ.get("DM_LINK_B") or DM_LINK
DATABASE_URL = os.environ.get("DATABASE_URL")
STATS_KEY = os.environ.get("STATS_KEY", "")
MAX_REPLIES_PER_HOUR = int(os.environ.get("MAX_REPLIES_PER_HOUR", "40"))

IG_GRAPH = "https://graph.instagram.com/v21.0"
FB_GRAPH = "https://graph.facebook.com/v21.0"

TRIGGER_WORD = os.environ.get("TRIGGER_WORD", "").strip()
TRIGGER = (
    re.compile(rf"\b{re.escape(TRIGGER_WORD)}\b", re.IGNORECASE) if TRIGGER_WORD else None
)


def matches_trigger(text: str) -> bool:
    return TRIGGER is None or bool(TRIGGER.search(text))


CLICK_BUTTON_TITLE = "Send it to me"
CLICK_PAYLOAD = "SEND_LINK"
LINK_BUTTON_TITLE = "Take the free test"

DM_TEXT = (
    "Thank you for your comment!\n\n"
    "The link to your free test is in the button below."
)
DM_TEXT_LINK = (
    "Thank you for your comment!\n\n"
    "Here is the link to your free test:\n{link}"
)
COPIES = {
    "m1": (
        "Thank you for commenting 🌿\n\n"
        "Here's one to try tonight: press the soft hollow at your temples (the Taiyang point) "
        "with gentle circles for 60 seconds before bed. It's traditionally used to calm a racing mind.\n\n"
        "Want the exact point for YOUR symptoms? Take the 30-second quiz:\n{link}"
    ),
    "m2": (
        "Thank you for your comment 💛\n\n"
        "Waking up at 3am, hot flashes, brain fog… in Chinese medicine, each symptom has its own point, "
        "and pressing the right one is what makes the difference.\n\n"
        "Find the one that matches what you feel right now (free, 30 seconds):\n{link}"
    ),
    "m3": (
        "Thank you for commenting ✨\n\n"
        "Your body has a pressure point for what you're feeling right now, "
        "and it's probably not where you'd expect.\n\n"
        "Answer 1 quick question and I'll show you yours:\n{link}"
    ),
    "m4": (
        "Thank you for your comment! 🌙\n\n"
        "Here's your free acupressure test. It takes 30 seconds and shows the exact point "
        "for your symptoms:\n{link}"
    ),
}
COPY_NAMES = {"m1": "1 — Valor primeiro", "m2": "2 — Dor", "m3": "3 — Curiosidade", "m4": "4 — Direta"}


def copy_text(variant: str) -> str:
    link = f"{DM_LINK.rstrip('/')}/?m={variant[1:]}"
    return COPIES[variant].format(link=link)


FALLBACK_TEXT = (
    "Thank you for your comment!\n\n"
    "Tap the button below and I'll send you the link to your free test."
)

PUBLIC_REPLIES = [
    "Just sent it to your DMs! 📩",
    "Check your inbox, I sent it over! 💛",
    "It's in your messages now, take a look! ✨",
    "Sent! Have a look at your DMs 🌿",
]

_processed = {}
_processed_lock = threading.Lock()
_DEDUPE_TTL = 3600

_sent_times = []
_sent_lock = threading.Lock()


TRACK_EVENTS = {"landed", "answered", "cta"}


def _db():
    return psycopg.connect(DATABASE_URL, connect_timeout=5, autocommit=True)


def init_db():
    if not (DATABASE_URL and psycopg):
        log.warning("DATABASE_URL not set, A/B stats disabled")
        return
    try:
        with _db() as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS ab_events (
                    id BIGSERIAL PRIMARY KEY,
                    ts TIMESTAMPTZ NOT NULL DEFAULT now(),
                    variant TEXT NOT NULL,
                    evt TEXT NOT NULL,
                    sid TEXT NOT NULL,
                    platform TEXT
                )"""
            )
    except Exception:
        log.exception("init_db failed")


def record(variant: str, evt: str, sid: str, platform: str = None):
    if not (DATABASE_URL and psycopg):
        return
    try:
        with _db() as conn:
            conn.execute(
                "INSERT INTO ab_events (variant, evt, sid, platform) VALUES (%s, %s, %s, %s)",
                (variant, evt, sid[:64], platform),
            )
    except Exception:
        log.exception("record failed (%s %s)", variant, evt)


def pick_variant() -> str:
    return random.choice(list(COPIES))


def already_processed(key: str) -> bool:
    now = time.time()
    with _processed_lock:
        for k in list(_processed):
            if now - _processed[k] > _DEDUPE_TTL:
                del _processed[k]
        if key in _processed:
            return True
        _processed[key] = now
        return False


def under_hourly_cap() -> bool:
    now = time.time()
    with _sent_lock:
        while _sent_times and now - _sent_times[0] > 3600:
            _sent_times.pop(0)
        if len(_sent_times) >= MAX_REPLIES_PER_HOUR:
            return False
        _sent_times.append(now)
        return True


PAGE_STYLE = (
    "font-family: -apple-system, Segoe UI, Roboto, sans-serif; max-width: 720px; "
    "margin: 40px auto; padding: 0 20px; line-height: 1.65; color: #222;"
)

PRIVACY_HTML = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Privacy Policy - Yinwell</title>
</head>
<body style="{PAGE_STYLE}">
<h1>Privacy Policy</h1>
<p><em>Last updated: September 21, 2026</em></p>

<p>This Privacy Policy explains how the Yinwell messaging assistant
("the app", "we", "us") handles information. The app automates replies to
comments and direct messages on the Instagram account
<strong>@yinwellhealth</strong> and the Facebook Page
<strong>Wellness for Midlife Women</strong>.</p>

<h2>1. Information we receive</h2>
<p>When you comment on one of our posts or interact with our direct
messages, Instagram and Facebook send us:</p>
<ul>
<li>the text of your comment or message;</li>
<li>your public username or name and your account ID on that platform;</li>
<li>the identifiers of the post and comment involved.</li>
</ul>
<p>We do not receive your password, your email address, your phone number,
your payment details, or your private information beyond the items above.</p>

<h2>2. How we use it</h2>
<p>We use this information only to reply automatically to your comment and
to send you the link to our free test by direct message. We do not use it
for advertising profiles, and we do not make automated decisions about you.</p>

<h2>3. Sharing</h2>
<p>We do not sell, rent, or share your information with third parties. It is
processed only through Meta's platform (Instagram and Facebook) and through
the hosting provider that runs the app.</p>

<h2>4. Retention</h2>
<p>The app does not keep a database of people or messages. Comment IDs are
kept in memory for about one hour to avoid replying twice, and are then
discarded. Our hosting provider keeps technical server logs for a limited
period, which may include the username and comment text, and then deletes
them automatically.</p>

<h2>5. Your choices and data deletion</h2>
<p>You can ask us to delete any information related to you at any time. See
<a href="/data-deletion">how to request data deletion</a>. You can also stop
receiving messages by simply not replying, or by removing the conversation
in Instagram or Messenger.</p>

<h2>6. Children</h2>
<p>The app is not directed to children under 13, and we do not knowingly
process their information.</p>

<h2>7. Changes</h2>
<p>We may update this policy. The date at the top shows the latest version.</p>

<h2>8. Contact</h2>
<p>Send us a direct message on Instagram at
<strong>@yinwellhealth</strong>, or on the Facebook Page
<strong>Wellness for Midlife Women</strong>.</p>
</body>
</html>"""

DATA_DELETION_HTML = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Data Deletion - Yinwell</title>
</head>
<body style="{PAGE_STYLE}">
<h1>Data Deletion Instructions</h1>
<p>The Yinwell messaging assistant does not keep a database of user
profiles. To request deletion of any information related to you:</p>
<ol>
<li>Send a direct message to <strong>@yinwellhealth</strong> on Instagram, or
to the Facebook Page <strong>Wellness for Midlife Women</strong>, with the
words "delete my data".</li>
<li>Tell us the username or name you used when you commented.</li>
<li>We will remove any information related to you, including entries in our
server logs where possible, and confirm by message within 30 days.</li>
</ol>
<p>You can also remove the app's access at any time in your Instagram or
Facebook settings, under "Apps and websites".</p>
<p><a href="/privacy">Back to the Privacy Policy</a></p>
</body>
</html>"""


@app.route("/privacy", methods=["GET"])
def privacy():
    return PRIVACY_HTML, 200


@app.route("/data-deletion", methods=["GET"])
def data_deletion():
    return DATA_DELETION_HTML, 200


@app.route("/t", methods=["POST", "OPTIONS"])
def track():
    resp = Response(status=204)
    resp.headers["Access-Control-Allow-Origin"] = "*"
    if request.method == "OPTIONS":
        resp.headers["Access-Control-Allow-Methods"] = "POST"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
        return resp
    try:
        body = json.loads(request.get_data(as_text=True) or "{}")
    except ValueError:
        return resp
    if not isinstance(body, dict):
        return resp
    v, e, sid = body.get("v"), body.get("e"), str(body.get("s") or "")
    if (v in COPIES or v in ("a", "b")) and e in TRACK_EVENTS and 0 < len(sid) <= 64:
        record(v, e, sid)
    return resp


STATS_SQL = """
SELECT variant,
  COUNT(*) FILTER (WHERE evt = 'dm_sent') AS dms,
  COUNT(DISTINCT sid) FILTER (WHERE evt = 'landed') AS landed,
  COUNT(DISTINCT sid) FILTER (WHERE evt = 'answered') AS answered,
  COUNT(DISTINCT sid) FILTER (WHERE evt = 'cta') AS cta
FROM ab_events GROUP BY variant
"""


def _pct(n, d):
    return f"{(100.0 * n / d):.1f}%" if d else "–"


STATS_PAGE = """<!doctype html><html lang="pt-BR"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="60"><title>Teste de copys — Yinwell</title>
<style>
body{font-family:-apple-system,Segoe UI,Roboto,sans-serif;background:#f6f5f2;color:#1d1d1f;margin:0;padding:24px 16px}
main{max-width:860px;margin:0 auto}
.wrap{overflow-x:auto;background:#fff;border:1px solid #e3e1dc;border-radius:12px}
table{border-collapse:collapse;width:100%;min-width:620px}
th,td{padding:12px 14px;border-bottom:1px solid #eee;text-align:right;font-variant-numeric:tabular-nums}
thead th{text-align:left;font-size:12px;color:#666;font-weight:600}
tbody th{text-align:left;font-weight:600}
p.note{color:#666;font-size:13px;line-height:1.5}
</style></head><body><main>
<h1>Teste de copys da DM — bot Yinwell</h1>__ERR__
<div class="wrap"><table><thead><tr><th>Versão</th><th>DMs enviadas</th><th>Entraram na página</th>
<th>% que entrou</th><th>Responderam o quiz</th><th>Clicaram em comprar</th><th>% compra / entrada</th></tr></thead>
<tbody>__ROWS__</tbody></table></div>
<p class="note">Cada comentário sorteia uma das 4 mensagens (25% cada). "Entraram", "responderam" e "clicaram" contam pessoas
diferentes (o mesmo navegador conta uma vez). A comparação mais justa é a coluna "% que entrou".
Com poucas dezenas de DMs a diferença ainda pode ser sorte: espere umas 100 DMs em cada mensagem antes de decidir.
A página atualiza sozinha a cada minuto.</p>
</main></body></html>"""


@app.route("/stats", methods=["GET"])
def stats():
    if not STATS_KEY or request.args.get("key") != STATS_KEY:
        return "forbidden", 403
    rows = {v: (0, 0, 0, 0) for v in COPIES}
    err = ""
    if DATABASE_URL and psycopg:
        try:
            with _db() as conn:
                for v, *nums in conn.execute(STATS_SQL).fetchall():
                    if v in rows:
                        rows[v] = tuple(nums)
        except Exception as exc:
            err = "<p style='color:#b00'>Erro ao ler o banco: " + html.escape(str(exc)) + "</p>"
    else:
        err = "<p style='color:#b00'>Banco não configurado.</p>"
    names = COPY_NAMES
    trs = ""
    for v in COPIES:
        dms, landed, answered, cta = rows[v]
        trs += (
            f"<tr><th>{names[v]}</th><td>{dms}</td><td>{landed}</td>"
            f"<td>{_pct(landed, dms)}</td><td>{answered}</td><td>{cta}</td>"
            f"<td>{_pct(cta, landed)}</td></tr>"
        )
    return STATS_PAGE.replace("__ERR__", err).replace("__ROWS__", trs), 200


@app.route("/webhook", methods=["GET"])
def verify():
    if (
        request.args.get("hub.mode") == "subscribe"
        and request.args.get("hub.verify_token") == VERIFY_TOKEN
    ):
        return request.args.get("hub.challenge", ""), 200
    return "forbidden", 403


@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json(force=True)
    log.info("Event received: %s", data)
    threading.Thread(target=process_event, args=(data,), daemon=True).start()
    return jsonify(status="ok"), 200


def process_event(data: dict):
    try:
        if data.get("object") == "page":
            process_facebook_event(data)
        else:
            process_instagram_event(data)
    except Exception:
        log.exception("Unexpected error while processing event")


def process_instagram_event(data: dict):
    for entry in data.get("entry", []):
        for event in entry.get("messaging", []):
            handle_messaging_event(entry, event)
        for change in entry.get("changes", []):
            if change.get("field") != "comments":
                continue
            value = change.get("value", {})
            comment_id = value.get("id")
            from_user = value.get("from", {})
            username = from_user.get("username")
            text = value.get("text") or ""

            own_ids = {IG_USER_ID, entry.get("id")}
            if (
                not comment_id
                or value.get("parent_id")
                or from_user.get("id") in own_ids
                or from_user.get("self_ig_scoped_id")
                or username == IG_USERNAME
            ):
                continue
            if not matches_trigger(text):
                continue
            if already_processed(comment_id):
                continue
            if not under_hourly_cap():
                log.warning("Hourly cap reached, skipping comment %s", comment_id)
                continue

            log.info("IG comment from %s (%s)", username, comment_id)
            ig_public_reply(comment_id, random.choice(PUBLIC_REPLIES))
            variant = pick_variant()
            if ig_private_reply(comment_id, variant):
                record(variant, "dm_sent", comment_id, "instagram")


def process_facebook_event(data: dict):
    if not FB_PAGE_TOKEN:
        return
    for entry in data.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            if (
                change.get("field") != "feed"
                or value.get("item") != "comment"
                or value.get("verb") != "add"
            ):
                continue
            comment_id = value.get("comment_id")
            from_user = value.get("from", {})
            text = value.get("message") or ""

            own_ids = {FB_PAGE_ID, entry.get("id")}
            if not comment_id or from_user.get("id") in own_ids:
                continue
            if value.get("parent_id") and value.get("parent_id") != value.get("post_id"):
                continue
            if not matches_trigger(text):
                continue
            if already_processed(comment_id):
                continue
            if not under_hourly_cap():
                log.warning("Hourly cap reached, skipping FB comment %s", comment_id)
                continue

            log.info("FB comment from %s (%s)", from_user.get("name"), comment_id)
            fb_public_reply(comment_id, random.choice(PUBLIC_REPLIES))
            variant = pick_variant()
            if fb_private_reply(comment_id, variant):
                record(variant, "dm_sent", comment_id, "facebook")


def ig_public_reply(comment_id: str, message: str):
    resp = requests.post(
        f"{IG_GRAPH}/{comment_id}/replies",
        params={"access_token": IG_TOKEN},
        data={"message": message},
    )
    if not resp.ok:
        log.error("IG public reply failed (%s): %s", comment_id, resp.text)


def _ig_post(body: dict) -> requests.Response:
    return requests.post(
        f"{IG_GRAPH}/me/messages", params={"access_token": IG_TOKEN}, json=body
    )


def link_button_template(text: str, link: str = None) -> dict:
    return {
        "attachment": {
            "type": "template",
            "payload": {
                "template_type": "button",
                "text": text,
                "buttons": [{"type": "web_url", "url": link or DM_LINK_A, "title": LINK_BUTTON_TITLE}],
            },
        }
    }


def ig_private_reply(comment_id: str, variant: str) -> bool:
    if variant in COPIES:
        resp = _ig_post({"recipient": {"comment_id": comment_id}, "message": {"text": copy_text(variant)}})
        if resp.ok:
            log.info("IG private reply sent (%s, copy %s)", comment_id, variant)
            return True
        log.error("IG private reply copy %s failed (%s): %s", variant, comment_id, resp.text)
        return False

    if variant == "b":
        text = DM_TEXT_LINK.format(link=DM_LINK_B)
        resp = _ig_post({"recipient": {"comment_id": comment_id}, "message": {"text": text}})
        if resp.ok:
            log.info("IG private reply sent (%s, variant b, link in text)", comment_id)
            return True
        log.error("IG private reply variant b failed (%s): %s", comment_id, resp.text)
        return False

    resp = _ig_post({"recipient": {"comment_id": comment_id}, "message": link_button_template(DM_TEXT, DM_LINK_A)})
    if resp.ok:
        log.info("IG private reply sent (%s, variant a, link button)", comment_id)
        return True
    log.error("IG private reply with link button refused (%s): %s", comment_id, resp.text)

    resp = _ig_post(
        {
            "recipient": {"comment_id": comment_id},
            "message": {
                "text": FALLBACK_TEXT,
                "quick_replies": [
                    {"content_type": "text", "title": CLICK_BUTTON_TITLE, "payload": CLICK_PAYLOAD}
                ],
            },
        }
    )
    if resp.ok:
        log.info("IG private reply sent (%s, variant a, two-step fallback)", comment_id)
        return True
    log.error("IG private reply fallback failed (%s): %s", comment_id, resp.text)
    return False


def handle_messaging_event(entry: dict, event: dict):
    sender_id = event.get("sender", {}).get("id")
    message = event.get("message") or {}
    postback = event.get("postback") or {}

    if message.get("is_echo") or sender_id in {IG_USER_ID, entry.get("id")}:
        return

    payload = (message.get("quick_reply") or {}).get("payload") or postback.get("payload")
    if payload != CLICK_PAYLOAD:
        return

    mid = message.get("mid") or postback.get("mid") or f"{sender_id}:{event.get('timestamp')}"
    if already_processed(f"click:{mid}"):
        return

    resp = _ig_post({"recipient": {"id": sender_id}, "message": link_button_template(DM_TEXT)})
    if resp.ok:
        log.info("IG final DM sent (%s)", sender_id)
    else:
        log.error("IG final DM failed (%s): %s", sender_id, resp.text)


def fb_public_reply(comment_id: str, message: str):
    resp = requests.post(
        f"{FB_GRAPH}/{comment_id}/comments",
        params={"access_token": FB_PAGE_TOKEN},
        data={"message": message.replace("DMs", "inbox").replace("direct", "inbox")},
    )
    if not resp.ok:
        log.error("FB public reply failed (%s): %s", comment_id, resp.text)


def fb_private_reply(comment_id: str, variant: str) -> bool:
    if variant in COPIES:
        message = {"text": copy_text(variant)}
    elif variant == "b":
        message = {"text": DM_TEXT_LINK.format(link=DM_LINK_B)}
    else:
        message = link_button_template(DM_TEXT, DM_LINK_A)
    resp = requests.post(
        f"{FB_GRAPH}/me/messages",
        params={"access_token": FB_PAGE_TOKEN},
        json={"recipient": {"comment_id": comment_id}, "message": message},
    )
    if resp.ok:
        log.info("FB private reply sent (%s, variant %s)", comment_id, variant)
        return True
    log.error("FB private reply failed (%s): %s", comment_id, resp.text)
    return False


init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
