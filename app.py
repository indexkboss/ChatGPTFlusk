import io
import json
import os
import re
import sqlite3
import time
from collections.abc import Iterator

import pandas as pd
from dotenv import load_dotenv
from fpdf import FPDF
from pypdf import PdfReader
from flask import Flask, Response, jsonify, render_template, request, send_file
from openai import (APIConnectionError, APIStatusError, AuthenticationError,
                    OpenAI, RateLimitError)

load_dotenv()
client = OpenAI(api_key=os.getenv("GROQ_API_KEY"),
                base_url="https://api.groq.com/openai/v1")
MODEL = "openai/gpt-oss-20b"
DB = "chat.db"
META, ERR = "\n§§META§§", "\n§§ERR§§"   # marqueurs de fin de flux

# Fonctionnalité 2 : un prompt système par domaine.
DOMAINS = {
    "general": "Tu es un assistant polyvalent. Réponds clairement.",
    "informatique": "Tu es un expert en informatique et développement logiciel. "
                    "Donne des réponses techniques précises avec des exemples de code.",
    "finance": "Tu es un expert en finance. Sois rigoureux, explique les "
               "hypothèses et rappelle que tu ne remplaces pas un conseiller.",
    "donnees": "Tu es un expert en analyse et gouvernance des données "
               "(qualité, SQL, statistiques, conformité).",
}

app = Flask(__name__)


# ---------- Base de données (fonctionnalité 3) ----------
def run(sql, args=()):
    """Exécute une requête SQLite, valide la transaction, retourne (lignes, dernier id)."""
    c = sqlite3.connect(DB)
    try:
        c.row_factory = sqlite3.Row
        with c:
            cur = c.execute(sql, args)
            return cur.fetchall(), cur.lastrowid
    finally:
        c.close()


def init_db():
    run("""CREATE TABLE IF NOT EXISTS conversations(
        id INTEGER PRIMARY KEY AUTOINCREMENT, domain TEXT NOT NULL,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
    run("""CREATE TABLE IF NOT EXISTS messages(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        conversation_id INTEGER NOT NULL REFERENCES conversations(id),
        role TEXT NOT NULL, content TEXT NOT NULL,
        rating INTEGER, response_ms INTEGER,
        tokens_in INTEGER, tokens_out INTEGER,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
    run("""CREATE TABLE IF NOT EXISTS documents(
        id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
    run("""CREATE TABLE IF NOT EXISTS chunks(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        doc_id INTEGER NOT NULL REFERENCES documents(id), text TEXT NOT NULL)""")


# ---------- Sources et références (fonctionnalité 10) ----------
def split_chunks(text: str) -> list[str]:
    """Découpe en extraits : paragraphes, ou blocs de 8 lignes si le paragraphe est long."""
    out = []
    for para in re.split(r"\n\s*\n", text):
        lines = [l for l in para.splitlines() if l.strip()]
        out += ["\n".join(lines[i:i + 8]) for i in range(0, len(lines), 8)]
    return out


def tokens(s: str) -> set:
    return set(re.findall(r"\w{3,}", s.lower()))


def retrieve(question, doc_ids, k, max_lines):
    """Retourne les k extraits les plus pertinents (mots communs) des sources cochées."""
    if not doc_ids:
        return []
    ph = ",".join("?" * len(doc_ids))
    rows = run(f"""SELECT c.text, d.name FROM chunks c JOIN documents d ON d.id=c.doc_id
                   WHERE d.id IN ({ph})""", tuple(doc_ids))[0]
    q = tokens(question)
    scored = sorted(((len(q & tokens(r["text"])), r) for r in rows), key=lambda x: -x[0])
    out = []
    for score, r in scored[:k]:
        if score == 0:
            break
        out.append({"n": len(out) + 1, "doc": r["name"],
                    "text": "\n".join(r["text"].splitlines()[:max_lines])})
    return out


@app.get("/sources")
def list_sources():
    rows = run("""SELECT d.id, d.name, COUNT(c.id) chunks FROM documents d
                  LEFT JOIN chunks c ON c.doc_id=d.id GROUP BY d.id ORDER BY d.id""")[0]
    return jsonify([dict(r) for r in rows])


@app.post("/sources")
def add_source():
    f = request.files.get("file")
    name = (f.filename if f else "").lower()
    try:
        if name.endswith(".pdf"):
            text = "\n\n".join(p.extract_text() or "" for p in PdfReader(f).pages)
        elif name.endswith((".txt", ".md")):
            text = f.read().decode("utf-8", errors="replace")
        else:
            return jsonify(error="Formats acceptés : TXT, MD, PDF."), 400
    except Exception:
        return jsonify(error="Document illisible."), 400
    chunks = split_chunks(text)
    if not chunks:
        return jsonify(error="Aucun texte exploitable dans ce document."), 400
    _, did = run("INSERT INTO documents(name) VALUES(?)", (f.filename,))
    for c in chunks:
        run("INSERT INTO chunks(doc_id,text) VALUES(?,?)", (did, c))
    return jsonify(id=did, chunks=len(chunks))


@app.delete("/sources/<int:did>")
def delete_source(did):
    run("DELETE FROM chunks WHERE doc_id=?", (did,))
    run("DELETE FROM documents WHERE id=?", (did,))
    return jsonify(ok=True)


def get_messages(conv_id):
    return run("SELECT * FROM messages WHERE conversation_id=? ORDER BY id", (conv_id,))[0]


@app.route("/")
def home():
    return render_template("index.html", domains=list(DOMAINS))


# ---------- Conversations (fonctionnalité 1) ----------
@app.post("/conversations")
def new_conversation():
    domain = (request.get_json(silent=True) or {}).get("domain", "general")
    if domain not in DOMAINS:
        domain = "general"
    _, cid = run("INSERT INTO conversations(domain) VALUES(?)", (domain,))
    return jsonify(id=cid)


@app.post("/prompt")
def prompt():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify(error="JSON invalide."), 400
    text, cid = (data.get("prompt") or "").strip(), data.get("conversation_id")
    conv = run("SELECT * FROM conversations WHERE id=?", (cid,))[0]
    if not text or not conv:
        return jsonify(error="Message vide ou conversation inconnue."), 400

    run("INSERT INTO messages(conversation_id,role,content) VALUES(?,?,?)",
        (cid, "user", text))
    try:
        ids = [int(i) for i in data.get("source_ids", [])]
        k = min(max(int(data.get("k", 3)), 1), 10)
        lines = min(max(int(data.get("lines", 5)), 1), 20)
    except (TypeError, ValueError):
        return jsonify(error="Paramètres de sources invalides."), 400
    sources = retrieve(text, ids, k, lines)
    system = DOMAINS[conv[0]["domain"]]
    if sources:
        system += ("\n\nAppuie-toi en priorité sur les sources ci-dessous et cite-les "
                   "avec [n]. Si elles ne suffisent pas, dis-le.\n" +
                   "\n".join(f"[{s['n']}] ({s['doc']}) {s['text']}" for s in sources))
    conversation = [{"role": "system", "content": system}] + [
        {"role": m["role"], "content": m["content"]} for m in get_messages(cid)]
    return Response(event_stream(cid, conversation, sources),
                    content_type="text/plain; charset=utf-8")


# ---------- Streaming + erreurs + performances (fonctionnalités 4 et 8) ----------
def event_stream(cid: int, conversation: list[dict], sources: list) -> Iterator[str]:
    start, text, usage = time.time(), "", None
    try:
        stream = client.chat.completions.create(
            model=MODEL, messages=conversation, stream=True,
            stream_options={"include_usage": True})
        for chunk in stream:
            usage = getattr(chunk, "usage", None) or usage
            if not chunk.choices:
                continue
            piece = chunk.choices[0].delta.content or ""
            if piece:
                text += piece
                yield piece
    except AuthenticationError:
        yield ERR + "Clé API invalide ou absente. Vérifiez GROQ_API_KEY dans le fichier .env."
        return
    except RateLimitError:
        yield ERR + "Quota ou limite de requêtes dépassé. Réessayez dans quelques instants."
        return
    except APIConnectionError:
        yield ERR + "Impossible de joindre le serveur IA. Vérifiez votre connexion internet."
        return
    except APIStatusError as e:
        yield ERR + f"Le service IA a renvoyé une erreur ({e.status_code})."
        return

    ms = int((time.time() - start) * 1000)
    t_in = getattr(usage, "prompt_tokens", None) or sum(len(m["content"]) for m in conversation) // 4
    t_out = getattr(usage, "completion_tokens", None) or len(text) // 4
    _, mid = run("""INSERT INTO messages(conversation_id,role,content,response_ms,tokens_in,tokens_out)
                    VALUES(?,?,?,?,?,?)""", (cid, "assistant", text, ms, t_in, t_out))
    yield META + json.dumps({"id": mid, "ms": ms, "tokens_in": t_in, "tokens_out": t_out,
                              "sources": sources})


@app.get("/stats/<int:cid>")
def stats(cid):
    r = run("""SELECT COUNT(*) n, AVG(response_ms) avg_ms, SUM(tokens_in) t_in,
               SUM(tokens_out) t_out, AVG(rating) avg_rating
               FROM messages WHERE conversation_id=? AND role='assistant'""", (cid,))[0][0]
    return jsonify({k: r[k] for k in r.keys()})


# ---------- Évaluation des réponses (fonctionnalité 9) ----------
@app.post("/rate")
def rate():
    d = request.get_json(silent=True) or {}
    if d.get("rating") not in (1, 2, 3):
        return jsonify(error="Note invalide (1 à 3)."), 400
    run("UPDATE messages SET rating=? WHERE id=? AND role='assistant'",
        (d["rating"], d.get("message_id")))
    return jsonify(ok=True)


# ---------- Export et résumé PDF (fonctionnalités 5 et 6) ----------
def clean(s: str) -> str:  # les polices PDF de base ne gèrent que le latin-1
    return s.encode("latin-1", "replace").decode("latin-1")


def make_pdf(title: str, blocks: list[tuple[str, str]]) -> bytes:
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 14)
    pdf.multi_cell(0, 10, clean(title))
    for who, body in blocks:
        pdf.set_font("Helvetica", "B", 11)
        pdf.multi_cell(0, 7, clean(who))
        pdf.set_font("Helvetica", "", 11)
        pdf.multi_cell(0, 6, clean(body))
        pdf.ln(2)
    return bytes(pdf.output())


@app.get("/export/<int:cid>.<fmt>")
def export(cid, fmt):
    msgs = get_messages(cid)
    if not msgs or fmt not in ("txt", "json", "pdf"):
        return jsonify(error="Conversation introuvable ou format non supporté."), 404
    label = lambda m: "Utilisateur" if m["role"] == "user" else "Assistant"
    if fmt == "json":
        data = json.dumps([{"role": m["role"], "content": m["content"], "rating": m["rating"]}
                           for m in msgs], ensure_ascii=False, indent=2).encode()
    elif fmt == "txt":
        data = "\n\n".join(f"[{label(m)}]\n{m['content']}" for m in msgs).encode()
    else:
        data = make_pdf(f"Conversation #{cid}", [(label(m), m["content"]) for m in msgs])
    return send_file(io.BytesIO(data), as_attachment=True, download_name=f"conversation_{cid}.{fmt}")


@app.get("/summary/<int:cid>.pdf")
def summary(cid):
    msgs = get_messages(cid)
    if not msgs:
        return jsonify(error="Conversation introuvable."), 404
    transcript = "\n".join(f"{m['role']}: {m['content']}" for m in msgs)
    try:
        r = client.chat.completions.create(model=MODEL, messages=[
            {"role": "system", "content": "Résume cette conversation en français : "
             "contexte, points clés, conclusions. Texte brut, sans Markdown."},
            {"role": "user", "content": transcript}])
    except AuthenticationError:
        return jsonify(error="Clé API invalide."), 401
    except RateLimitError:
        return jsonify(error="Quota dépassé."), 429
    except APIConnectionError:
        return jsonify(error="Problème de connexion au service IA."), 503
    pdf = make_pdf(f"Résumé de la conversation #{cid}", [("Résumé", r.choices[0].message.content)])
    return send_file(io.BytesIO(pdf), as_attachment=True, download_name=f"resume_{cid}.pdf")


# ---------- Analyse et gouvernance des données (fonctionnalité 7) ----------
@app.post("/analyze")
def analyze():
    f = request.files.get("file")
    if not f:
        return jsonify(error="Aucun fichier reçu."), 400
    try:
        name = f.filename.lower()
        df = pd.read_csv(f) if name.endswith(".csv") else pd.read_excel(f)
    except Exception:
        return jsonify(error="Fichier illisible. Formats acceptés : CSV, XLSX."), 400
    if df.empty:
        return jsonify(error="Le fichier ne contient aucune donnée."), 400

    rows, cols = df.shape
    missing = df.isna().sum()
    completeness = 100 * (1 - missing.sum() / df.size)
    dups = int(df.duplicated().sum())
    dup_pct = 100 * dups / rows

    outliers, inconsistent = {}, []
    for c in df.select_dtypes("number"):
        q1, q3 = df[c].quantile([.25, .75])
        iqr = q3 - q1
        n = int(((df[c] < q1 - 1.5 * iqr) | (df[c] > q3 + 1.5 * iqr)).sum())
        if n:
            outliers[c] = n
    for c in df.select_dtypes("object"):
        s = df[c].dropna().astype(str)
        if s.nunique() > s.str.strip().str.lower().nunique():
            inconsistent.append(c)  # variantes de casse/espaces : "Paris", "paris "
    constant = [c for c in df if df[c].nunique(dropna=True) <= 1]

    score = max(0, round(completeness * .5 + (100 - dup_pct) * .3
                         + 100 * (1 - sum(outliers.values()) / df.size) * .2))
    md = [f"## Rapport qualité : {f.filename}",
          f"**{rows} lignes × {cols} colonnes** — score global : **{score}/100**", "",
          f"- Complétude : **{completeness:.1f} %**",
          f"- Doublons : **{dups}** ({dup_pct:.1f} %)",
          f"- Colonnes constantes : {', '.join(constant) or 'aucune'}",
          f"- Colonnes incohérentes (casse/espaces) : {', '.join(inconsistent) or 'aucune'}",
          f"- Anomalies numériques (IQR) : "
          f"{', '.join(f'{k} ({v})' for k, v in outliers.items()) or 'aucune'}", "",
          "**Valeurs manquantes par colonne**", ""]
    md += [f"- {c} : {int(n)} ({100 * n / rows:.1f} %)" for c, n in missing.items() if n]
    if not missing.any():
        md.append("- aucune")
    return jsonify(report="\n".join(md))


if __name__ == "__main__":
    init_db()
    app.run(debug=True)
