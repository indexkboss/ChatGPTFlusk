const META = "\n§§META§§";
const ERR = "\n§§ERR§§";
// Couleurs de notation : 1 = insuffisante, 2 = moyenne, 3 = satisfaisante.
const COLORS = { 1: "#ef4444", 2: "#f59e0b", 3: "#22c55e" };
const LABELS = { 1: "Insuffisante", 2: "Moyenne", 3: "Satisfaisante" };

let conversationId = null;
const $ = (s) => document.querySelector(s);
const converter = () => new showdown.Converter();
const render = (md) => DOMPurify.sanitize(converter().makeHtml(md));

function createMessageElement() {
    const clone = $("#chat-template").cloneNode(true);
    clone.id = "";
    $("#gpt-output").appendChild(clone);
    clone.classList.remove("hidden");
    return clone.querySelector(".message");
}

function appendMessage(message, markdown = false) {
    const el = createMessageElement();
    if (markdown) el.innerHTML = render(message);
    else el.innerText = message;
    return el;
}

async function ensureConversation() {
    if (conversationId) return conversationId;
    const r = await fetch("/conversations", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ domain: $("#domain").value }),
    });
    conversationId = (await r.json()).id;
    $("#domain").disabled = true; // le domaine est fixé pour toute la conversation
    return conversationId;
}

// Sépare le texte reçu en contenu, erreur éventuelle et métadonnées.
function parseStream(text) {
    const e = text.indexOf(ERR), m = text.indexOf(META);
    const cuts = [e, m].filter((i) => i >= 0);
    const end = cuts.length ? Math.min(...cuts) : text.length;
    let meta = null;
    if (m >= 0) { try { meta = JSON.parse(text.slice(m + META.length)); } catch {} }
    return { body: text.slice(0, end), error: e >= 0 ? text.slice(e + ERR.length) : null, meta };
}

async function readResponseChunks(reader, el) {
    const decoder = new TextDecoder("utf-8");
    let text = "";
    try {
        while (true) {
            const { done, value } = await reader.read();
            if (done) break;
            text += decoder.decode(value, { stream: true });
            el.innerHTML = render(parseStream(text).body);
        }
    } finally {
        reader.releaseLock();
    }
    const { body, error, meta } = parseStream(text + decoder.decode());
    el.innerHTML = render(body);
    if (error) {
        el.parentElement.style.borderLeft = "6px solid #ef4444";
        el.innerHTML += `<p class="mt-2 text-red-600">⚠️ ${DOMPurify.sanitize(error)}</p>`;
    } else if (meta) {
        addFooter(el, meta);
        refreshStats();
    }
}

// Pied de réponse : temps, tokens et boutons de notation colorés.
function addFooter(el, meta) {
    const footer = document.createElement("div");
    footer.className = "mt-3 flex items-center gap-2 text-xs text-slate-500";
    footer.innerHTML = `<span>⏱ ${(meta.ms / 1000).toFixed(2)} s · 🔤 ${meta.tokens_in} → ${meta.tokens_out} tokens</span>`;
    for (const r of [1, 2, 3]) {
        const b = document.createElement("button");
        b.type = "button";
        b.title = LABELS[r];
        b.className = "h-5 w-5 rounded-full border border-white shadow";
        b.style.background = COLORS[r];
        b.addEventListener("click", async () => {
            await fetch("/rate", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ message_id: meta.id, rating: r }),
            });
            el.parentElement.style.borderLeft = `6px solid ${COLORS[r]}`;
            refreshStats();
        });
        footer.appendChild(b);
    }
    el.parentElement.appendChild(footer);
    if (meta.sources && meta.sources.length) {
        const det = document.createElement("details");
        det.className = "mt-2 text-xs text-slate-600";
        const sum = document.createElement("summary");
        sum.textContent = `Sources utilisées (${meta.sources.length})`;
        det.appendChild(sum);
        for (const s of meta.sources) {
            const p = document.createElement("p");
            p.className = "mt-1 whitespace-pre-wrap";
            p.textContent = `[${s.n}] ${s.doc} — ${s.text}`;
            det.appendChild(p);
        }
        el.parentElement.appendChild(det);
    }
}

async function loadSources() {
    const list = await (await fetch("/sources")).json();
    const box = $("#sources-list");
    box.innerHTML = "";
    for (const d of list) {
        const row = document.createElement("label");
        row.className = "flex items-center gap-2";
        row.innerHTML = '<input type="checkbox" checked><span></span>';
        row.querySelector("input").value = d.id;
        row.querySelector("span").textContent = `${d.name} (${d.chunks} extraits)`;
        const del = document.createElement("button");
        del.type = "button";
        del.textContent = "✕";
        del.className = "text-red-500";
        del.addEventListener("click", async (e) => {
            e.preventDefault();
            await fetch(`/sources/${d.id}`, { method: "DELETE" });
            loadSources();
        });
        row.appendChild(del);
        box.appendChild(row);
    }
}

function selectedSources() {
    return [...document.querySelectorAll("#sources-list input:checked")].map((i) => +i.value);
}

async function refreshStats() {
    if (!conversationId) return;
    const s = await (await fetch(`/stats/${conversationId}`)).json();
    $("#stats").textContent = s.n
        ? `${s.n} réponse(s) · moy. ${(s.avg_ms / 1000).toFixed(2)} s · ${s.t_in + s.t_out} tokens` +
          (s.avg_rating ? ` · note moy. ${s.avg_rating.toFixed(1)}/3` : "")
        : "";
}

function newConversation() {
    conversationId = null;
    $("#gpt-output").innerHTML = "";
    $("#domain").disabled = false;
    $("#stats").textContent = "";
}

document.addEventListener("DOMContentLoaded", () => {
    const form = $("#prompt-form");
    const button = form.querySelector("button");

    form.addEventListener("submit", async (event) => {
        event.preventDefault();
        const prompt = form.elements.prompt.value.trim();
        if (!prompt || button.disabled) return;
        appendMessage(prompt);
        form.reset();
        button.disabled = true;
        try {
            const cid = await ensureConversation();
            const response = await fetch("/prompt", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    conversation_id: cid, prompt,
                    source_ids: selectedSources(),
                    k: +$("#k").value || 3, lines: +$("#lines").value || 5,
                }),
            });
            if (!response.ok) throw new Error((await response.json()).error);
            const output = appendMessage("");
            await readResponseChunks(response.body.getReader(), output);
        } catch (error) {
            appendMessage("⚠️ Erreur : impossible de récupérer la réponse. " + error.message);
            console.error(error);
        } finally {
            button.disabled = false;
        }
    });

    $("#new-chat").addEventListener("click", newConversation);
    loadSources();
    $("#source-file").addEventListener("change", async (e) => {
        const fd = new FormData();
        fd.append("file", e.target.files[0]);
        const r = await fetch("/sources", { method: "POST", body: fd });
        if (!r.ok) alert("⚠️ " + (await r.json()).error);
        e.target.value = "";
        loadSources();
    });

    $("#export").addEventListener("change", (e) => {
        if (e.target.value && conversationId) location.href = `/export/${conversationId}.${e.target.value}`;
        else if (e.target.value) alert("Aucune conversation à exporter.");
        e.target.value = "";
    });

    $("#summary").addEventListener("click", async () => {
        if (!conversationId) return alert("Aucune conversation à résumer.");
        const r = await fetch(`/summary/${conversationId}.pdf`);
        if (!r.ok) return alert("⚠️ " + (await r.json()).error);
        const a = Object.assign(document.createElement("a"), {
            href: URL.createObjectURL(await r.blob()), download: `resume_${conversationId}.pdf`,
        });
        a.click();
    });

    $("#analyze").addEventListener("change", async (e) => {
        const file = e.target.files[0];
        if (!file) return;
        const fd = new FormData();
        fd.append("file", file);
        const r = await fetch("/analyze", { method: "POST", body: fd });
        const data = await r.json();
        appendMessage(r.ok ? data.report : "⚠️ " + data.error, r.ok);
        e.target.value = "";
    });
});
