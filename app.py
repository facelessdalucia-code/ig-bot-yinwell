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


COPY_BUTTON_TITLE = "Take the free quiz"


def copy_link(variant: str) -> str:
    return f"{DM_LINK.rstrip('/')}/?m={variant[1:]}"


def copy_text(variant: str) -> str:
    return COPIES[variant].format(link=copy_link(variant))



FORMATS = {"m1": "Só link no texto", "m2": "Link no texto + botão"}
FORMAT_TEST_START = os.environ.get("FORMAT_TEST_START", "2100-01-01T00:00:00Z")
FORMAT_TEST_END = "2026-10-04T13:41:00Z"


PAGE_URL = os.environ.get("PAGE_URL", "https://yinwell-method.pages.dev/")
QUIZ_URL = os.environ.get("QUIZ_URL", "https://yinwell-method.pages.dev/quiz/")  # quiz com checkout embutido (05/10)
PAGE_TEST = {"tq": "DM → quiz → página", "tp": "DM → página direta"}
PAGE_TEST_START = os.environ.get("PAGE_TEST_START", "2026-10-05T01:00:00Z")
DM_PAGE = (
    "Thank you for commenting 🌿\n\n"
    "Here's one to try tonight: press the soft hollow at your temples (the Taiyang point) "
    "with gentle circles for 60 seconds before bed. It's traditionally used to calm a racing mind.\n\n"
    "I put every point I show — 26 of them, photographed on the body — in one simple guide:\n{link}"
)


def page_test_message(variant: str):
    # teste quiz × página (desde 05/10): tq = um dos 4 textos com link do quiz; tp = ponto grátis + link da página
    if variant == "tp":
        text = DM_PAGE.format(link=f"{PAGE_URL.rstrip('/')}/?x=tp")
    else:
        m = random.choice(list(COPIES))
        text = COPIES[m].format(link=f"{QUIZ_URL.rstrip('/')}/?m={m[1:]}&x=tq")
    return {"text": text}, text


def format_message(variant: str):
    if variant in PAGE_TEST:
        return page_test_message(variant)
    # O texto sorteia um dos 4 textos; o formato (com ou sem botão) é o que está em teste.
    link = f"{DM_LINK.rstrip('/')}/?m={variant[1:]}"
    text = COPIES[random.choice(list(COPIES))].format(link=link)
    if variant == "m2":
        msg = {
            "attachment": {
                "type": "template",
                "payload": {
                    "template_type": "button",
                    "text": text,
                    "buttons": [{"type": "web_url", "url": link, "title": COPY_BUTTON_TITLE}],
                },
            }
        }
    else:
        msg = {"text": text}
    return msg, text


def copy_button(variant: str) -> dict:
    # Botão de link: em "solicitação de mensagem" o Instagram não deixa link
    # escrito no texto clicável, mas o botão funciona.
    text = COPIES[variant].replace("\n{link}", "").strip()
    return {
        "attachment": {
            "type": "template",
            "payload": {
                "template_type": "button",
                "text": text,
                "buttons": [{"type": "web_url", "url": copy_link(variant), "title": COPY_BUTTON_TITLE}],
            },
        }
    }


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


TRACK_EVENTS = {"landed", "started", "answered", "cta", "ck", "paid"}
LAYOUTS = ("intro", "direct")


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
            conn.execute("ALTER TABLE ab_events ADD COLUMN IF NOT EXISTS grp TEXT")
    except Exception:
        log.exception("init_db failed")


def record(variant: str, evt: str, sid: str, platform: str = None, grp: str = None):
    if not (DATABASE_URL and psycopg):
        return
    try:
        with _db() as conn:
            conn.execute(
                "INSERT INTO ab_events (variant, evt, sid, platform, grp) VALUES (%s, %s, %s, %s, %s)",
                (variant, evt, sid[:64], platform, grp),
            )
    except Exception:
        log.exception("record failed (%s %s)", variant, evt)


def pick_variant() -> str:
    # teste de formato encerrado em 04/10 (só link no texto venceu). Desde 05/10: quiz × página direta, 50/50
    return random.choice(list(PAGE_TEST))


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
    g = body.get("g") if body.get("g") in LAYOUTS else None
    if (v in COPIES or v in ("a", "b") or v in PAGE_TEST) and e in TRACK_EVENTS and 0 < len(sid) <= 64:
        record(v, e, sid, grp=g)
    return resp


STATS_SQL = """
SELECT variant,
  COUNT(*) FILTER (WHERE evt = 'dm_sent') AS dms,
  COUNT(DISTINCT sid) FILTER (WHERE evt = 'landed') AS landed,
  COUNT(DISTINCT sid) FILTER (WHERE evt = 'answered') AS answered,
  COUNT(DISTINCT sid) FILTER (WHERE evt = 'cta') AS cta
FROM ab_events WHERE ts < %(start)s GROUP BY variant
"""


FORMAT_SQL = """
SELECT variant,
  COUNT(*) FILTER (WHERE evt = 'dm_sent') AS dms,
  COUNT(DISTINCT sid) FILTER (WHERE evt = 'landed') AS landed,
  COUNT(DISTINCT sid) FILTER (WHERE evt = 'answered') AS answered,
  COUNT(DISTINCT sid) FILTER (WHERE evt = 'cta') AS cta
FROM ab_events WHERE ts >= %(start)s AND ts < %(end)s AND variant IN ('m1', 'm2') GROUP BY variant
"""


LAYOUT_SQL = """
SELECT grp,
  COUNT(DISTINCT sid) FILTER (WHERE evt = 'landed') AS landed,
  COUNT(DISTINCT sid) FILTER (WHERE evt = 'started') AS started,
  COUNT(DISTINCT sid) FILTER (WHERE evt = 'answered') AS answered,
  COUNT(DISTINCT sid) FILTER (WHERE evt = 'cta') AS cta
FROM ab_events WHERE grp IS NOT NULL GROUP BY grp
"""
LAYOUT_NAMES = {"intro": "Com tela de abertura (como era)", "direct": "Direto na pergunta"}

PAGE_SQL = """
SELECT variant,
  COUNT(*) FILTER (WHERE evt = 'dm_sent') AS dms,
  COUNT(DISTINCT sid) FILTER (WHERE evt = 'landed') AS landed,
  COUNT(DISTINCT sid) FILTER (WHERE evt = 'answered') AS answered,
  COUNT(DISTINCT sid) FILTER (WHERE evt = 'ck') AS ck,
  COUNT(DISTINCT sid) FILTER (WHERE evt = 'paid') AS paid
FROM ab_events WHERE ts >= %(start)s AND variant IN ('tq', 'tp') GROUP BY variant
"""


def _pct(n, d):
    return f"{(100.0 * n / d):.1f}%" if d else "–"


STATS_PAGE = """<!doctype html><html lang="pt-BR"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="60"><title>Testes — Yinwell</title>
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
<h1>Bot Yinwell — testes</h1>__ERR__
<h2>Teste ativo: quiz × página de venda direta (desde 05/10)</h2>
<div class="wrap"><table><thead><tr><th>Caminho</th><th>DMs enviadas</th><th>Entraram</th><th>% que entrou</th>
<th>Responderam o quiz</th><th>Abriram o checkout</th><th>Compraram</th><th>% compra / DM</th></tr></thead>
<tbody>__PAGE__</tbody></table></div>
<p class="note">Cada comentário sorteia 50/50. <b>Quiz</b>: um dos 4 textos com o link do quiz; o botão do resultado leva pra página nova.
<b>Página direta</b>: DM entrega um ponto grátis (Taiyang) + link da página nova. "Compraram" conta compras pagas no Stripe
(o produto principal; bump/upsell aparecem no painel do Cloudflare). A coluna que decide é "% compra / DM".</p>
<h2>Teste do formato da DM (encerrado em 04/10: venceu só link no texto)</h2>
<div class="wrap"><table><thead><tr><th>Formato</th><th>DMs enviadas</th><th>Entraram na página</th>
<th>% que entrou</th><th>Responderam o quiz</th><th>Clicaram em comprar</th><th>% compra / entrada</th></tr></thead>
<tbody>__FORMAT__</tbody></table></div>
<p class="note">De __START__ até 04/10. Desde então todas as DMs vão só com o link no texto, com os 4 textos sorteados (25% cada). Cada comentário sorteia 50/50 entre mandar só o link escrito no texto ou o link no texto
com um botão embaixo. O texto da mensagem é sorteado entre os 4 textos nos dois formatos, então ele pesa igual dos dois lados.
A coluna que decide é "% que entrou". Espere umas 100 DMs em cada formato antes de decidir.</p>
<h2 style="margin-top:36px">Teste dos 4 textos da DM (encerrado em 01/10)</h2>
<div class="wrap"><table><thead><tr><th>Versão</th><th>DMs enviadas</th><th>Entraram na página</th>
<th>% que entrou</th><th>Responderam o quiz</th><th>Clicaram em comprar</th><th>% compra / entrada</th></tr></thead>
<tbody>__ROWS__</tbody></table></div>
<h2 style="margin-top:36px">Teste da tela de abertura do quiz</h2>
<div class="wrap"><table><thead><tr><th>Grupo</th><th>Entraram</th><th>Clicaram em "Start"</th>
<th>Responderam o quiz</th><th>% que respondeu</th><th>Clicaram em comprar</th><th>% compra / entrada</th></tr></thead>
<tbody>__LAYOUT__</tbody></table></div>
<p class="note">Quem vem da DM é sorteado 50/50: metade vê a tela de abertura com o botão "Start the Quiz"
(como antes) e metade cai direto na pergunta. A coluna que decide é "% que respondeu" (respondeu ÷ entrou).
"Clicaram em Start" só existe no grupo com tela de abertura e mostra quantos passam dela.</p>
<p class="note">Números até o início do teste de formato. Cada comentário sorteava uma das 4 mensagens (25% cada); entre 27/09 e 28/09 a mensagem 1 recebeu 50%. Compare pela coluna de porcentagem, não pelo total. "Entraram", "responderam" e "clicaram" contam pessoas
diferentes (o mesmo navegador conta uma vez). A comparação mais justa é a coluna "% que entrou".
Com poucas dezenas de DMs a diferença ainda pode ser sorte: espere umas 100 DMs em cada mensagem antes de decidir.
A página atualiza sozinha a cada minuto.</p>
</main></body></html>"""


@app.route("/insights", methods=["GET"])
def insights():
    """Só leitura, pro CENTRAL V: perfil, alcance 7d e últimos posts com views/comentários."""
    if not STATS_KEY or request.args.get("key") != STATS_KEY:
        return "forbidden", 403
    tok = IG_TOKEN
    def g(path, **p):
        p["access_token"] = tok
        try:
            return requests.get(f"https://graph.instagram.com/v21.0/{path}", params=p, timeout=20).json()
        except Exception as e:
            return {"error": {"message": str(e)}}
    out = {"perfil": g("me", fields="id,username,followers_count,media_count")}
    since = int(time.time()) - 7 * 86400
    ins = g("me/insights", metric="reach,profile_views,accounts_engaged,follower_count", period="day", metric_type="total_value", since=since)
    out["insights_7d"] = {m["name"]: (m.get("total_value") or {}).get("value") for m in ins.get("data", [])}
    if "error" in ins:
        out["insights_erro"] = ins["error"].get("message")
    posts = []
    for p in g("me/media", fields="id,caption,media_product_type,timestamp,like_count,comments_count,permalink,thumbnail_url,media_url", limit=15).get("data", []):
        mi = g(f"{p['id']}/insights", metric="reach,views,saved,shares")
        v = {m["name"]: (m.get("values") or [{}])[0].get("value") for m in mi.get("data", [])}
        posts.append({"id": p["id"], "quando": p.get("timestamp"), "tipo": p.get("media_product_type"),
                      "titulo": (p.get("caption") or "").split("\n")[0][:90], "link": p.get("permalink"),
                      "thumb": p.get("thumbnail_url") or p.get("media_url"),
                      "curtidas": p.get("like_count"), "comentarios": p.get("comments_count"),
                      "views": v.get("views"), "alcance": v.get("reach"), "salvos": v.get("saved"), "compart": v.get("shares")})
    out["posts"] = posts
    return jsonify(out)


@app.route("/insights_fb", methods=["GET"])
def insights_fb():
    """Só leitura, pro CENTRAL V: página do Facebook (seguidores, alcance 7d, posts/reels recentes, agendados)."""
    if not STATS_KEY or request.args.get("key") != STATS_KEY:
        return "forbidden", 403
    tok = FB_PAGE_TOKEN
    if not tok:
        return jsonify({"erro": "sem token da página"})
    def g(path, **p):
        p["access_token"] = tok
        try:
            return requests.get(f"https://graph.facebook.com/v21.0/{path}", params=p, timeout=20).json()
        except Exception as e:
            return {"error": {"message": str(e)}}
    out = {"perfil": g("me", fields="id,name,followers_count,fan_count")}
    since = int(time.time()) - 7 * 86400
    ins = {}
    for m in ["page_impressions_unique", "page_post_engagements", "page_video_views", "page_daily_follows_unique"]:
        r = g("me/insights", metric=m, period="day", since=since, until=int(time.time()))
        if "error" in r:
            ins[m] = {"erro": r["error"].get("message")}
        else:
            ins[m] = sum((v.get("value") or 0) for d in r.get("data", []) for v in d.get("values", []) if isinstance(v.get("value"), (int, float)))
    out["insights_7d"] = ins
    posts = []
    r = g("me/posts", fields="id,message,created_time,permalink_url,comments.summary(true).limit(0),reactions.summary(true).limit(0),shares", limit=15)
    if "error" in r:
        out["posts_erro"] = r["error"].get("message")
    for p in r.get("data", []):
        posts.append({"id": p["id"], "quando": p.get("created_time"), "titulo": (p.get("message") or "").split("\n")[0][:90], "link": p.get("permalink_url"),
                      "comentarios": ((p.get("comments") or {}).get("summary") or {}).get("total_count"),
                      "curtidas": ((p.get("reactions") or {}).get("summary") or {}).get("total_count"), "compart": (p.get("shares") or {}).get("count")})
    v = g("me/video_reels", fields="id,description,created_time,permalink_url,video_insights", limit=15)
    reels = []
    if "error" in v:
        out["reels_erro"] = v["error"].get("message")
    for x in v.get("data", []):
        vi = {i.get("name"): (i.get("values") or [{}])[0].get("value") for i in (x.get("video_insights") or {}).get("data", [])}
        reels.append({"id": x["id"], "quando": x.get("created_time"), "titulo": (x.get("description") or "").split("\n")[0][:90],
                      "link": ("https://www.facebook.com" + x["permalink_url"]) if (x.get("permalink_url") or "").startswith("/") else x.get("permalink_url"),
                      "views": vi.get("blue_reels_play_count") or vi.get("fb_reels_total_plays"), "alcance": vi.get("post_impressions_unique")})
    out["posts"] = posts
    out["reels"] = reels
    s = g("me/scheduled_posts", fields="id,message,scheduled_publish_time,created_time", limit=50)
    out["agendados"] = [{"id": x["id"], "quando": x.get("scheduled_publish_time"), "titulo": (x.get("message") or "").split("\n")[0][:90]} for x in s.get("data", [])]
    if "error" in s:
        out["agendados_erro"] = s["error"].get("message")
    return jsonify(out)


@app.route("/stats", methods=["GET"])
def stats():
    if not STATS_KEY or request.args.get("key") != STATS_KEY:
        return "forbidden", 403
    rows = {v: (0, 0, 0, 0) for v in COPIES}
    frows = {v: (0, 0, 0, 0) for v in FORMATS}
    lrows = {g: (0, 0, 0, 0) for g in LAYOUTS}
    prows = {v: (0, 0, 0, 0, 0) for v in PAGE_TEST}
    err = ""
    if DATABASE_URL and psycopg:
        try:
            with _db() as conn:
                for v, *nums in conn.execute(STATS_SQL, {"start": FORMAT_TEST_START}).fetchall():
                    if v in rows:
                        rows[v] = tuple(nums)
                for v, *nums in conn.execute(FORMAT_SQL, {"start": FORMAT_TEST_START, "end": FORMAT_TEST_END}).fetchall():
                    if v in frows:
                        frows[v] = tuple(nums)
                for g, *nums in conn.execute(LAYOUT_SQL).fetchall():
                    if g in lrows:
                        lrows[g] = tuple(nums)
                for v, *nums in conn.execute(PAGE_SQL, {"start": PAGE_TEST_START}).fetchall():
                    if v in prows:
                        prows[v] = tuple(nums)
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
    ltrs = ""
    for g in LAYOUTS:
        landed, started, answered, cta = lrows[g]
        start_cell = str(started) if g == "intro" else "—"
        ltrs += (
            f"<tr><th>{LAYOUT_NAMES[g]}</th><td>{landed}</td><td>{start_cell}</td>"
            f"<td>{answered}</td><td>{_pct(answered, landed)}</td><td>{cta}</td>"
            f"<td>{_pct(cta, landed)}</td></tr>"
        )
    ftrs = ""
    for v in FORMATS:
        dms, landed, answered, cta = frows[v]
        ftrs += (
            f"<tr><th>{FORMATS[v]}</th><td>{dms}</td><td>{landed}</td>"
            f"<td>{_pct(landed, dms)}</td><td>{answered}</td><td>{cta}</td>"
            f"<td>{_pct(cta, landed)}</td></tr>"
        )
    ptrs = ""
    for v in PAGE_TEST:
        dms, landed, answered, ck, paid = prows[v]
        quiz_cell = str(answered) if v == "tq" else "—"
        ptrs += (
            f"<tr><th>{PAGE_TEST[v]}</th><td>{dms}</td><td>{landed}</td><td>{_pct(landed, dms)}</td>"
            f"<td>{quiz_cell}</td><td>{ck}</td><td>{paid}</td><td>{_pct(paid, dms)}</td></tr>"
        )
    start_label = html.escape(FORMAT_TEST_START.replace("T", " ")[:16]) + " UTC"
    page = STATS_PAGE.replace("__ERR__", err).replace("__ROWS__", trs).replace("__LAYOUT__", ltrs).replace("__PAGE__", ptrs)
    return page.replace("__FORMAT__", ftrs).replace("__START__", start_label), 200


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
    import perfis
    for entry in data.get("entry", []):
        if str(entry.get("id")) in perfis.PERFIS and str(entry.get("id")) != str(IG_USER_ID):
            perfis.processar({"entry": [entry]})  # Wen, Mei, Hua... (mesmo app, outro perfil)
            continue
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
    if variant in FORMATS or variant in PAGE_TEST:
        msg, text = format_message(variant)
        resp = _ig_post({"recipient": {"comment_id": comment_id}, "message": msg})
        if resp.ok:
            log.info("IG private reply sent (%s, format %s)", comment_id, variant)
            return True
        log.error("IG private reply format %s refused (%s): %s", variant, comment_id, resp.text)
        if variant in ("m1", "tq", "tp"):
            return False
        resp = _ig_post({"recipient": {"comment_id": comment_id}, "message": {"text": text}})
        if resp.ok:
            log.info("IG private reply sent (%s, copy %s, text fallback)", comment_id, variant)
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
    if variant in FORMATS or variant in PAGE_TEST:
        message = format_message(variant)[0]
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
