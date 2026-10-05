"""Outros perfis no mesmo app da Meta (respondedor): comentário com a palavra → resposta pública + DM com ponto grátis + link.
Config na env PERFIS_JSON {ig_user_id: {perfil, token, username, palavra, link, ponto, onde, produto}}. Teste T10 (rA/rB) e métricas no painel central (Worker)."""
import hashlib
import json
import logging
import os
import random
import threading
import time

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("perfis")

VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN", "")
PERFIS = json.loads(os.environ.get("PERFIS_JSON", "{}"))
FUNIL = os.environ.get("FUNIL_API", "https://yinwell-checkout.yinwell.workers.dev")
MAX_HORA = int(os.environ.get("MAX_REPLIES_PER_HOUR", "40"))
G = "https://graph.instagram.com/v21.0"

PUBLICAS = ["Sent ✅ check your DMs", "Just sent it 🌿", "Sent! Try it tonight ✨", "Check your DMs 💌"]
DM = ("Here's your free point 🌿\n\n{ponto}: {onde}. Press 60 seconds, breathing slowly.\n\n"
      "{produto} is here 👉 {link}\n\nDid you find the point? Reply YES and I'll tell you the best time to do it.")
LEMBRETE = ("Do it tonight, right before bed: 60 seconds each side 🌙 Most people notice it on day 2 or 3.\n\n"
            "The full guide is here when you want it 👉 {link}")

_vistos, _envios, _lock = {}, {}, threading.Lock()


def lado(uid: str) -> str:
    return "rB" if int(hashlib.md5(uid.encode()).hexdigest(), 16) % 2 else "rA"


def contar(perfil: str, v: str, e: str):
    try:
        requests.post(FUNIL + "/t", data=json.dumps({"perfil": perfil, "v": v, "e": e}), timeout=5)
    except Exception:  # noqa: BLE001
        pass


def uma_vez(chave: str, ttl=86400) -> bool:
    now = time.time()
    with _lock:
        for k in [k for k, t in _vistos.items() if now - t > ttl]:
            del _vistos[k]
        if chave in _vistos:
            return False
        _vistos[chave] = now
        return True


def dentro_do_limite(conta: str) -> bool:
    now = time.time()
    with _lock:
        l = [t for t in _envios.get(conta, []) if now - t < 3600]
        if len(l) >= MAX_HORA:
            _envios[conta] = l
            return False
        l.append(now)
        _envios[conta] = l
        return True


def link_com(cfg: dict, v: str) -> str:
    base = cfg["link"]
    return base + ("&" if "?" in base else "?") + "x=" + v


def post(cfg, caminho, **kw):
    r = requests.post(f"{G}/{caminho}", params={"access_token": cfg["token"]}, timeout=15, **kw)
    if not r.ok:
        log.error("%s %s → %s", cfg["perfil"], caminho, r.text[:300])
    return r.ok


def comentario(conta: str, cfg: dict, v: dict):
    cid, de, texto = v.get("id"), v.get("from", {}), (v.get("text") or "")
    if not cid or v.get("parent_id") or de.get("id") == conta or de.get("username") == cfg.get("username"):
        return
    if cfg.get("palavra") and cfg["palavra"].lower() not in texto.lower():
        return
    if not uma_vez("c" + cid) or not dentro_do_limite(conta):
        return
    var = lado(de.get("id") or cid)
    post(cfg, f"{cid}/replies", data={"message": random.choice(PUBLICAS)})
    msg = DM.format(ponto=cfg["ponto"], onde=cfg["onde"], produto=cfg["produto"], link=link_com(cfg, var))
    if post(cfg, "me/messages", json={"recipient": {"comment_id": cid}, "message": {"text": msg}}):
        contar(cfg["perfil"], var, "dm_sent")
        log.info("%s: DM enviada (%s, %s)", cfg["perfil"], de.get("username"), var)


def mensagem(conta: str, cfg: dict, ev: dict):
    m, rem = ev.get("message") or {}, (ev.get("sender") or {}).get("id")
    if not rem or rem == conta or m.get("is_echo") or not m.get("text"):
        return
    var = lado(rem)
    contar(cfg["perfil"], var, "dm_reply")
    if var == "rB" and uma_vez("r" + rem, ttl=7 * 86400):
        if post(cfg, "me/messages", json={"recipient": {"id": rem}, "message": {"text": LEMBRETE.format(link=link_com(cfg, var))}}):
            contar(cfg["perfil"], var, "reminder")


def processar(data: dict):
    try:
        for entry in data.get("entry", []):
            conta = str(entry.get("id"))
            cfg = PERFIS.get(conta)
            if not cfg:
                log.warning("conta sem config: %s", conta)
                continue
            for ch in entry.get("changes", []):
                if ch.get("field") == "comments":
                    comentario(conta, cfg, ch.get("value", {}))
            for ev in entry.get("messaging", []):
                mensagem(conta, cfg, ev)
    except Exception:  # noqa: BLE001
        log.exception("erro processando evento")


