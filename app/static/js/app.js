// ================================================
// app/static/js/app.js
// Frontend controller: connection status, sync, chat (SSE streaming),
// and the pending-send envelope confirmation flow.
// ================================================

const el = (id) => document.getElementById(id);

let chatReady = false;
let pendingSend = null;
let currentSessionId = "default";
let ledger = JSON.parse(localStorage.getItem("correspondent_ledger") || "[]"); // display-only cache; server is source of truth for auth

function saveLedger() {
    localStorage.setItem("correspondent_ledger", JSON.stringify(ledger));
    renderLedger();
}

function logEntry(role, content) {
    ledger.push({ timestamp: new Date().toLocaleString(), role, content });
    saveLedger();
}

function renderLedger() {
    el("ledgerCount").textContent = ledger.length;
    const box = el("ledgerEntries");
    if (ledger.length === 0) {
        box.innerHTML = '<div class="mono-meta">No correspondence logged yet.</div>';
        return;
    }
    box.innerHTML = [...ledger].reverse().map(entry => `
        <div class="ledger-entry">
            <div class="mono-meta">${entry.timestamp} · ${entry.role === "user" ? "YOU" : "ASSISTANT"}</div>
            <div>${escapeHtml(entry.content)}</div>
        </div>
        <div class="perforation"></div>
    `).join("");
}

function escapeHtml(str) {
    const d = document.createElement("div");
    d.innerText = str;
    return d.innerHTML;
}

async function api(path, options = {}) {
    const res = await fetch(`/api${path}`, {
        headers: { "Content-Type": "application/json" },
        ...options,
    });
    if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body.detail || `Request failed (${res.status})`);
    }
    return res.json();
}

// ---------------- Status / connection ----------------

async function refreshStatus() {
    const status = await api("/status");
    const credBox = el("credBox");
    const authBox = el("authBox");
    const connectedBox = el("connectedBox");

    credBox.classList.toggle("hidden", status.credentials_uploaded);
    authBox.classList.toggle("hidden", !status.credentials_uploaded || status.gmail_connected);
    connectedBox.classList.toggle("hidden", !status.gmail_connected);

    el("redirectUriText").textContent = status.redirect_uri;

    el("fetchBtn").disabled = !status.gmail_connected;
    el("startBtn").disabled = !status.gmail_connected;

    chatReady = status.chat_ready;
    updateStartButton();
    el("chatSection").classList.toggle("hidden", !chatReady);
    el("chatPlaceholder").classList.toggle("hidden", chatReady);

    return status;
}

function updateStartButton() {
    const btn = el("startBtn");
    btn.textContent = chatReady ? "●  Chatbot ready" : "○  Start chatbot";
    btn.className = chatReady ? "full" : "full primary";
}

el("credFile").addEventListener("change", async (e) => {
    const file = e.target.files[0];
    if (!file) return;
    const formData = new FormData();
    formData.append("file", file);
    try {
        await fetch("/api/oauth/credentials", { method: "POST", body: formData });
        showAlert("credAlert", "success", "Credentials saved. Click Connect Gmail below.");
        await refreshStatus();
    } catch (err) {
        showAlert("credAlert", "error", `Failed to save credentials — ${err.message}`);
    }
});

el("connectBtn").addEventListener("click", async () => {
    try {
        const { auth_url } = await api("/oauth/url");
        window.location.href = auth_url;
    } catch (err) {
        showAlert("authAlert", "error", err.message);
    }
});

el("disconnectBtn").addEventListener("click", async () => {
    await api("/oauth/disconnect", { method: "POST" });
    chatReady = false;
    await refreshStatus();
});

el("fetchBtn").addEventListener("click", async () => {
    const btn = el("fetchBtn");
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner"></span> Syncing...';
    try {
        const result = await api("/sync", { method: "POST" });
        showAlert("syncAlert", "success", result.message);
    } catch (err) {
        showAlert("syncAlert", "error", `Sync failed — ${err.message}`);
    } finally {
        btn.disabled = false;
        btn.textContent = "↻  Fetch new mail";
        refreshStatus();
    }
});

el("startBtn").addEventListener("click", async () => {
    if (chatReady) return;
    const btn = el("startBtn");
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner"></span> Loading retrievers...';
    try {
        await api("/chatbot/start", { method: "POST" });
        chatReady = true;
    } catch (err) {
        showAlert("syncAlert", "error", `Failed to load chatbot — ${err.message}`);
    } finally {
        btn.disabled = false;
        await refreshStatus();
    }
});

el("newChatBtn").addEventListener("click", async () => {
    if (!chatReady) return;
    try {
        const result = await api("/chat/new", { method: "POST" });
        currentSessionId = result.session_id;
        el("chatWindow").innerHTML = "";
        pendingSend = null;
        renderPendingSend();
    } catch (err) {
        showAlert("syncAlert", "error", `Couldn't start a new chat — ${err.message}`);
    }
});

function showAlert(id, kind, message) {
    const box = el(id);
    box.textContent = message;
    box.className = `alert alert-${kind}`;
    box.classList.remove("hidden");
    setTimeout(() => box.classList.add("hidden"), 6000);
}

// ---------------- Chat ----------------

function appendMessage(role, content) {
    const wrap = document.createElement("div");
    wrap.className = `msg ${role}`;
    wrap.innerHTML = `<div class="role">${role === "user" ? "YOU" : "ASSISTANT"}</div><div class="content"></div>`;
    wrap.querySelector(".content").textContent = content;
    el("chatWindow").appendChild(wrap);
    el("chatWindow").scrollTop = el("chatWindow").scrollHeight;
    return wrap.querySelector(".content");
}

function renderPendingSend() {
    const box = el("pendingBox");
    if (!pendingSend) {
        box.classList.add("hidden");
        box.innerHTML = "";
        return;
    }
    box.classList.remove("hidden");
    box.innerHTML = `
        <div class="envelope-card">
            <div class="mono-meta">TO&nbsp;&nbsp;${escapeHtml(pendingSend.recipient)}</div>
            <div class="mono-meta">SUBJECT&nbsp;&nbsp;${escapeHtml(pendingSend.subject)}</div>
            <textarea id="pendingBody" disabled>${escapeHtml(pendingSend.body)}</textarea>
            <div class="btn-row" style="margin-top:10px;">
                <button class="primary" id="confirmSendBtn">✓  Confirm & send</button>
                <button id="discardBtn">✕  Discard draft</button>
            </div>
        </div>
    `;
    el("confirmSendBtn").addEventListener("click", confirmSend);
    el("discardBtn").addEventListener("click", discardDraft);
    el("chatInput").disabled = true;
    el("chatInput").placeholder = "Resolve the pending draft above first";
}

async function confirmSend() {
    const btn = el("confirmSendBtn");
    btn.disabled = true;
    btn.textContent = "Sending...";
    try {
        await api("/send-email", {
            method: "POST",
            body: JSON.stringify(pendingSend),
        });
        const msg = `Sent to ${pendingSend.recipient}.`;
        appendMessage("assistant", msg);
        logEntry("assistant", msg);
    } catch (err) {
        const msg = `Failed to send — ${err.message}`;
        appendMessage("assistant", msg);
        logEntry("assistant", msg);
    } finally {
        pendingSend = null;
        renderPendingSend();
        el("chatInput").disabled = false;
        el("chatInput").placeholder = "Ask about your emails, or draft one...";
    }
}

function discardDraft() {
    const msg = "Draft discarded — not sent.";
    appendMessage("assistant", msg);
    logEntry("assistant", msg);
    pendingSend = null;
    renderPendingSend();
    el("chatInput").disabled = false;
    el("chatInput").placeholder = "Ask about your emails, or draft one...";
}

async function sendChat() {
    const input = el("chatInput");
    const text = input.value.trim();
    if (!text || pendingSend) return;

    input.value = "";
    appendMessage("user", text);
    logEntry("user", text);

    const assistantContentEl = appendMessage("assistant", "");
    assistantContentEl.innerHTML = '<span class="spinner"></span> Reading through your inbox...';

    try {
        const res = await fetch("/api/chat/stream", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ message: text, session_id: currentSessionId }),
        });
        if (!res.ok || !res.body) throw new Error(`Request failed (${res.status})`);

        const reader = res.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";
        let started = false;
        let finalPayload = null;

        while (true) {
            const { done, value } = await reader.read();
            if (done) break;
            buffer += decoder.decode(value, { stream: true });

            let boundary;
            while ((boundary = buffer.indexOf("\n\n")) !== -1) {
                const rawEvent = buffer.slice(0, boundary);
                buffer = buffer.slice(boundary + 2);
                const lines = rawEvent.split("\n");
                const eventLine = lines.find((l) => l.startsWith("event:"));
                const dataLine = lines.find((l) => l.startsWith("data:"));
                if (!dataLine) continue;
                const eventType = eventLine ? eventLine.replace("event:", "").trim() : "message";
                const data = JSON.parse(dataLine.replace("data:", "").trim());

                if (eventType === "chunk") {
                    if (!started) {
                        assistantContentEl.textContent = "";
                        started = true;
                    }
                    assistantContentEl.textContent += data.text;
                    el("chatWindow").scrollTop = el("chatWindow").scrollHeight;
                } else if (eventType === "final") {
                    finalPayload = data;
                }
            }
        }

        if (finalPayload) {
            if (finalPayload.type === "pending_send") {
                assistantContentEl.textContent = `Draft ready for ${finalPayload.recipient} — review it below.`;
                pendingSend = finalPayload;
                renderPendingSend();
            } else {
                assistantContentEl.textContent = finalPayload.content;
            }
            logEntry("assistant", finalPayload.content || assistantContentEl.textContent);
        }
    } catch (err) {
        assistantContentEl.textContent = `Something went wrong — ${err.message}`;
        logEntry("assistant", assistantContentEl.textContent);
    }
}

el("chatInput").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        sendChat();
    }
});
el("sendBtn").addEventListener("click", sendChat);

el("clearLedgerBtn").addEventListener("click", () => {
    ledger = [];
    saveLedger();
    el("chatWindow").innerHTML = "";
});

el("exportLedgerBtn").addEventListener("click", () => {
    const blob = new Blob([JSON.stringify(ledger, null, 2)], { type: "application/json" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = "conversation_ledger.json";
    a.click();
});

// ---------------- Boot ----------------

(async function init() {
    const params = new URLSearchParams(window.location.search);
    if (params.get("connected")) showAlert("authAlert", "success", "Gmail connected.");
    if (params.get("oauth_error")) showAlert("authAlert", "error", `Authorization failed — ${params.get("oauth_error")}`);
    if (params.toString()) window.history.replaceState({}, "", "/");

    renderLedger();
    try {
        await refreshStatus();
    } catch (err) {
        console.error(err);
    }
})();
