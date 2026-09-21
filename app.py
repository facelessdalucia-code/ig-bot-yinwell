import os
import re
import logging
import random
import threading
import time

import requests
from flask import Flask, request, jsonify

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
MAX_REPLIES_PER_HOUR = int(os.environ.get("MAX_REPLIES_PER_HOUR", "40"))

IG_GRAPH = "https://graph.instagram.com/v21.0"
FB_GRAPH = "https://graph.facebook.com/v21.0"

TRIGGER = re.compile(r"\btest\b", re.IGNORECASE)

CLICK_BUTTON_TITLE = "Send it to me"
CLICK_PAYLOAD = "SEND_LINK"
LINK_BUTTON_TITLE = "Take the free test"

DM_TEXT = (
    "Thank you for your comment!\n\n"
    "The link to your free test is in the button below."
)
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
            if not TRIGGER.search(text):
                continue
            if already_processed(comment_id):
                continue
            if not under_hourly_cap():
                log.warning("Hourly cap reached, skipping comment %s", comment_id)
                continue

            log.info("IG comment from %s (%s)", username, comment_id)
            ig_public_reply(comment_id, random.choice(PUBLIC_REPLIES))
            ig_private_reply(comment_id)


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
            if not TRIGGER.search(text):
                continue
            if already_processed(comment_id):
                continue
            if not under_hourly_cap():
                log.warning("Hourly cap reached, skipping FB comment %s", comment_id)
                continue

            log.info("FB comment from %s (%s)", from_user.get("name"), comment_id)
            fb_public_reply(comment_id, random.choice(PUBLIC_REPLIES))
            fb_private_reply(comment_id)


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


def link_button_template(text: str) -> dict:
    return {
        "attachment": {
            "type": "template",
            "payload": {
                "template_type": "button",
                "text": text,
                "buttons": [{"type": "web_url", "url": DM_LINK, "title": LINK_BUTTON_TITLE}],
            },
        }
    }


def ig_private_reply(comment_id: str):
    resp = _ig_post({"recipient": {"comment_id": comment_id}, "message": link_button_template(DM_TEXT)})
    if resp.ok:
        log.info("IG private reply sent (%s, link button)", comment_id)
        return
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
        log.info("IG private reply sent (%s, two-step fallback)", comment_id)
    else:
        log.error("IG private reply fallback failed (%s): %s", comment_id, resp.text)


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


def fb_private_reply(comment_id: str):
    resp = requests.post(
        f"{FB_GRAPH}/me/messages",
        params={"access_token": FB_PAGE_TOKEN},
        json={"recipient": {"comment_id": comment_id}, "message": link_button_template(DM_TEXT)},
    )
    if resp.ok:
        log.info("FB private reply sent (%s)", comment_id)
    else:
        log.error("FB private reply failed (%s): %s", comment_id, resp.text)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
