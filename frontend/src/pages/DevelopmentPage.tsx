import { useCallback, useEffect, useMemo, useState } from "react";
import axios from "axios";
import {
    sendChatMessage,
    fetchChatSession,
    fetchChatSessions,
    fetchChatSessionStatus,
    type ChatMessage,
    type ChatSessionSummary,
} from "../api/client";

function formatSessionLabel(summary: ChatSessionSummary): string {
    const timestamp = summary.updated_at ? new Date(summary.updated_at).toLocaleString() : summary.session_id;
    const preview = summary.last_message_preview ? summary.last_message_preview.replace(/\s+/g, " ").trim() : "";
    if (preview.length === 0) {
        return timestamp;
    }
    const truncated = preview.length > 60 ? `${preview.slice(0, 57)}…` : preview;
    return `${timestamp} – ${truncated}`;
}

export function DevelopmentPage(): JSX.Element {
    const [sessions, setSessions] = useState<ChatSessionSummary[]>([]);
    const [sessionsLoading, setSessionsLoading] = useState<boolean>(false);
    const [chatMessages, setChatMessages] = useState<ChatMessage[]>([]);
    const [chatInput, setChatInput] = useState<string>("");
    const [chatLoading, setChatLoading] = useState<boolean>(false);
    const [chatSession, setChatSession] = useState<string | undefined>(undefined);
    const [chatError, setChatError] = useState<string | null>(null);
    const [pollingTimeout, setPollingTimeout] = useState<NodeJS.Timeout | null>(null);

    const selectedSession = useMemo(
        () => sessions.find((session) => session.session_id === chatSession),
        [sessions, chatSession],
    );

    const loadSessionMessages = useCallback(async (sessionId: string | undefined, options?: { silent?: boolean }) => {
        if (!sessionId) {
            return;
        }
        if (!options?.silent) {
            setChatLoading(true);
        }
        setChatError(null);
        try {
            const detail = await fetchChatSession(sessionId);
            setChatSession(detail.session_id);
            setChatMessages(detail.messages);
        } catch (err) {
            console.error(err);
            if (!options?.silent) {
                setChatError("Sitzung konnte nicht geladen werden.");
            }
        } finally {
            if (!options?.silent) {
                setChatLoading(false);
            }
        }
    }, []);

    const refreshSessions = useCallback(
        async (options?: { selectLatest?: boolean }) => {
            setSessionsLoading(true);
            try {
                const items = await fetchChatSessions();
                setSessions(items);
                if (options?.selectLatest && items.length > 0) {
                    void loadSessionMessages(items[0].session_id);
                }
            } catch (err) {
                console.error(err);
                setChatError("Codex-Sitzungen konnten nicht geladen werden.");
            } finally {
                setSessionsLoading(false);
            }
        },
        [loadSessionMessages],
    );

    useEffect(() => {
        void refreshSessions({ selectLatest: true });
    }, [refreshSessions]);
    
    // Cleanup polling on unmount
    useEffect(() => {
        return () => {
            if (pollingTimeout) {
                clearTimeout(pollingTimeout);
            }
        };
    }, [pollingTimeout]);

    const handleChatSubmit = async (event: React.FormEvent) => {
        event.preventDefault();
        if (!chatInput.trim() || chatLoading) {
            return;
        }
        const messageText = chatInput.trim();
        const userMessage: ChatMessage = { role: "user", content: messageText };
        const previousMessages = [...chatMessages];
        setChatMessages((prev) => [...prev, userMessage]);
        setChatInput("");
        setChatLoading(true);
        setChatError(null);
        try {
            const response = await sendChatMessage({
                message: messageText,
                session_id: chatSession,
                history: [...previousMessages, userMessage],
            });
            setChatSession(response.session_id);
            
            // Cancel any existing polling
            if (pollingTimeout) {
                clearTimeout(pollingTimeout);
                setPollingTimeout(null);
            }
            
            // Poll for session updates until completion or error
            const pollInterval = 1000; // Poll every second
            const maxPollAttempts = 300; // Max 5 minutes (300 * 1000ms)
            let pollAttempts = 0;
            let currentTimeout: NodeJS.Timeout | null = null;
            
            const pollSession = async (): Promise<void> => {
                try {
                    const status = await fetchChatSessionStatus(response.session_id);
                    if (status.status === "completed" || status.status === "error") {
                        // Session is done, load final messages
                        await loadSessionMessages(response.session_id, { silent: true });
                        void refreshSessions();
                        setChatLoading(false);
                        if (currentTimeout) {
                            clearTimeout(currentTimeout);
                            currentTimeout = null;
                        }
                        setPollingTimeout(null);
                        return;
                    }
                    // Still running, reload messages to show any progress
                    if (status.message_count > previousMessages.length + 1) {
                        await loadSessionMessages(response.session_id, { silent: true });
                    }
                    // Continue polling if not done and under max attempts
                    if (pollAttempts < maxPollAttempts && status.is_running) {
                        pollAttempts++;
                        currentTimeout = setTimeout(pollSession, pollInterval);
                        setPollingTimeout(currentTimeout);
                    } else if (pollAttempts >= maxPollAttempts) {
                        // Timeout - load current state
                        setChatError("Timeout beim Warten auf Antwort. Bitte Sitzung aktualisieren.");
                        await loadSessionMessages(response.session_id, { silent: true });
                        setChatLoading(false);
                        setPollingTimeout(null);
                    }
                } catch (err) {
                    console.error("Error polling session status:", err);
                    // On error, try to load current session state
                    try {
                        await loadSessionMessages(response.session_id, { silent: true });
                    } catch (loadErr) {
                        console.error("Error loading session:", loadErr);
                    }
                    setChatLoading(false);
                    setPollingTimeout(null);
                }
            };
            
            // Start polling after a short delay to allow backend to start processing
            currentTimeout = setTimeout(pollSession, 500);
            setPollingTimeout(currentTimeout);
            
            // Also refresh sessions list
            void refreshSessions();
        } catch (err) {
            console.error(err);
            if (axios.isAxiosError(err)) {
                const status = err.response?.status;
                const detail = typeof err.response?.data === "object" && err.response?.data !== null && "detail" in err.response.data
                    ? String(err.response.data.detail)
                    : err.message;
                
                let errorMessage = `Chat-Anfrage fehlgeschlagen`;
                if (status === 502) {
                    errorMessage = `Codex-Verbindungsfehler: ${detail}`;
                } else if (status === 429) {
                    errorMessage = `Rate Limit erreicht: ${detail}`;
                } else if (status === 404) {
                    errorMessage = `Session nicht gefunden: ${detail}`;
                } else if (detail.includes("OPENAI_API_KEY")) {
                    errorMessage = `OpenAI API Key nicht konfiguriert. Bitte in settings.json konfigurieren.`;
                } else if (detail.includes("Codex-Binary")) {
                    errorMessage = `Codex-Binary nicht gefunden. Verwende OpenAI Responses API.`;
                } else {
                    errorMessage = `${errorMessage} (${status ?? "net"}): ${detail}`;
                }
                setChatError(errorMessage);
            } else {
                setChatError("Chat-Anfrage fehlgeschlagen. Bitte später erneut versuchen.");
            }
            setChatMessages(previousMessages);
            setChatLoading(false);
        }
    };

    const resetChat = () => {
        setChatMessages([]);
        setChatSession(undefined);
        setChatError(null);
        setChatInput("");
    };

    const handleSessionChange = (event: React.ChangeEvent<HTMLSelectElement>) => {
        const selectedId = event.target.value;
        if (!selectedId) {
            resetChat();
            return;
        }
        void loadSessionMessages(selectedId);
    };

    const isSubmitDisabled = chatLoading || !chatInput.trim();

    return (
        <section className="page">
            <header className="page__header">
                <div>
                    <h2>Development</h2>
                    <p>Codex-Assistent und Terminal für schnelle Iterationen.</p>
                </div>
            </header>

            <div className="grid grid--cols-2 grid--gap-lg">
                <div className="card">
                    <h3>Codex Chat</h3>
                    <p className="card__hint">
                        Gespräche werden gespeichert und sind auch nach Neustart verfügbar.
                        <button type="button" className="link-button" onClick={resetChat} disabled={chatLoading}>
                            Neue Sitzung
                        </button>
                    </p>
                    <div className="chat-panel">
                        <div className="chat-panel__session-bar">
                            <div className="chat-panel__session-field">
                                <label htmlFor="chat-session-select">Sitzung</label>
                                <select
                                    id="chat-session-select"
                                    value={chatSession ?? ""}
                                    onChange={handleSessionChange}
                                    disabled={chatLoading || sessionsLoading}
                                >
                                    <option value="">(neue Sitzung)</option>
                                    {sessions.map((session) => (
                                        <option key={session.session_id} value={session.session_id}>
                                            {formatSessionLabel(session)}
                                        </option>
                                    ))}
                                </select>
                            </div>
                            <button
                                type="button"
                                className="chat-panel__session-refresh"
                                onClick={() => {
                                    void refreshSessions();
                                }}
                                disabled={sessionsLoading}
                            >
                                {sessionsLoading ? "Lade…" : "Aktualisieren"}
                            </button>
                        </div>
                        {selectedSession && (
                            <p className="chat-panel__session-meta">
                                Zuletzt aktualisiert: {new Date(selectedSession.updated_at).toLocaleString()} · Nachrichten: {selectedSession.message_count}
                            </p>
                        )}
                        <div className="chat-panel__messages">
                            {chatMessages.length === 0 && <p className="chat-panel__placeholder">Noch keine Nachrichten.</p>}
                            {chatMessages.map((msg, idx) => (
                                <div key={`${msg.role}-${idx}`} className={`chat-panel__message chat-panel__message--${msg.role}`}>
                                    <span>{msg.content}</span>
                                </div>
                            ))}
                            {chatLoading && (
                                <div className="chat-panel__message chat-panel__message--assistant chat-panel__typing">
                                    <span>Codex denkt nach<span className="chat-panel__typing-dots" aria-hidden="true" /></span>
                                </div>
                            )}
                        </div>
                        {chatError && (
                            <div className="chat-panel__error">
                                {chatError}
                                <button
                                    type="button"
                                    className="link-button"
                                    onClick={() => setChatError(null)}
                                    style={{ marginLeft: "8px", fontSize: "0.9em" }}
                                >
                                    ✕
                                </button>
                            </div>
                        )}
                        <form className="chat-panel__form" onSubmit={handleChatSubmit}>
                            <textarea
                                value={chatInput}
                                onChange={(event) => setChatInput(event.target.value)}
                                placeholder="Frage eingeben..."
                                rows={3}
                                disabled={chatLoading}
                            />
                            <button type="submit" disabled={isSubmitDisabled}>
                                {chatLoading ? "Sende..." : "Senden"}
                            </button>
                        </form>
                    </div>
                </div>
                <div className="card">
                    <h3>Terminal</h3>
                    <p className="card__hint">Interaktives Server-Terminal (nur mit Auth erhältlich).</p>
                    <div className="terminal-frame">
                        <iframe title="Server Terminal" src="/terminal/" />
                    </div>
                </div>
            </div>
        </section>
    );
}
