import { useCallback, useEffect, useState, useRef } from "react";
import {
  fetchAutoOptimizerStatus,
  fetchAutoOptimizerResults,
  fetchAutoOptimizerHistory,
  fetchAutoOptimizerBestParams,
  fetchAutoOptimizerLog,
  startAutoOptimizer,
  stopAutoOptimizer,
  fetchFocusedStrategies,
  setFocusedStrategies,
  fetchBranchesOverview,
  type AutoOptimizerStatus,
  type AutoOptimizerTestResult,
  type FocusedStrategiesResponse,
  type BranchOverview,
} from "../api/client";
import { ScoreTimelineChart } from "../components/ScoreTimelineChart";
import { PnLTradeChart } from "../components/PnLTradeChart";
import { WinrateChart } from "../components/WinrateChart";
import { LLMDecisionsTimeline } from "../components/LLMDecisionsTimeline";
import { ParameterVariationsChart } from "../components/ParameterVariationsChart";
import { BaselineComparisonChart } from "../components/BaselineComparisonChart";
import { OptimizationProcessFlow } from "../components/OptimizationProcessFlow";

function formatDate(value: string | null | undefined): string {
  if (!value) return "—";
  try {
    return new Date(value).toLocaleString();
  } catch {
    return value;
  }
}

function formatNumber(value: number | null | undefined, digits = 2): string {
  if (value == null || Number.isNaN(value)) return "—";
  return value.toFixed(digits);
}

function formatPercent(value: number | null | undefined): string {
  if (value == null || Number.isNaN(value)) return "—";
  return `${(value * 100).toFixed(1)}%`;
}

export function AutoOptimizerPage(): JSX.Element {
  const [status, setStatus] = useState<AutoOptimizerStatus | null>(null);
  const [results, setResults] = useState<AutoOptimizerTestResult[]>([]);
  const [history, setHistory] = useState<AutoOptimizerTestResult[]>([]);
  const [bestParams, setBestParams] = useState<Record<string, Record<string, unknown>>>({});
  const [bestParamsByMode, setBestParamsByMode] = useState<Record<string, Record<string, Record<string, unknown>>>>({});
  const [selectedBestParamsStrategy, setSelectedBestParamsStrategy] = useState<string>("");
  const [selectedBestParamsMode, setSelectedBestParamsMode] = useState<string>("combined");
  const [loading, setLoading] = useState<boolean>(false);
  const [initialLoadComplete, setInitialLoadComplete] = useState<boolean>(false);
  const [isRefreshing, setIsRefreshing] = useState<boolean>(false);
  const initialLoadCompleteRef = useRef<boolean>(false);
  const [error, setError] = useState<string | null>(null);
  const [selectedStrategy, setSelectedStrategy] = useState<string>("");
  const [logs, setLogs] = useState<string[]>([]);
  const [logLoading, setLogLoading] = useState<boolean>(false);
  const logContainerRef = useRef<HTMLPreElement | null>(null);
  const shouldAutoScrollRef = useRef<boolean>(true);
  const [focusedStrategiesData, setFocusedStrategiesData] = useState<FocusedStrategiesResponse | null>(null);
  const [selectedFocusedStrategies, setSelectedFocusedStrategies] = useState<Set<string>>(new Set());
  const [focusedStrategiesLoading, setFocusedStrategiesLoading] = useState<boolean>(false);
  const [activeTab, setActiveTab] = useState<string>("overview");
  const [branchesOverview, setBranchesOverview] = useState<BranchOverview[]>([]);
  const [branchesLoading, setBranchesLoading] = useState<boolean>(false);
  // Paginierung für Top-Ergebnisse
  const [resultsPage, setResultsPage] = useState<number>(1);
  const [resultsTotalCount, setResultsTotalCount] = useState<number>(0);
  const [resultsLoading, setResultsLoading] = useState<boolean>(false);
  // Paginierung für Historie
  const [historyPage, setHistoryPage] = useState<number>(1);
  const [historyTotalCount, setHistoryTotalCount] = useState<number>(0);
  const [historyLoading, setHistoryLoading] = useState<boolean>(false);
  const RESULTS_PER_PAGE = 30;
  const HISTORY_PER_PAGE = 50;

  useEffect(() => {
    const modes = Object.keys(bestParamsByMode);
    if (modes.length > 0) {
      if (!selectedBestParamsMode || !bestParamsByMode[selectedBestParamsMode]) {
        setSelectedBestParamsMode(modes[0]);
      }
      return;
    }
    if (Object.keys(bestParams).length > 0) {
      setSelectedBestParamsMode("combined");
    } else {
      setSelectedBestParamsMode("");
    }
  }, [bestParamsByMode, bestParams, selectedBestParamsMode]);

  useEffect(() => {
    const paramsSource =
      Object.keys(bestParamsByMode).length > 0
        ? (bestParamsByMode[selectedBestParamsMode] || bestParamsByMode[Object.keys(bestParamsByMode)[0]] || {})
        : bestParams;
    const strategies = Object.keys(paramsSource);
    if (strategies.length === 0) {
      setSelectedBestParamsStrategy("");
      return;
    }
    if (!selectedBestParamsStrategy || !paramsSource[selectedBestParamsStrategy]) {
      setSelectedBestParamsStrategy(strategies[0]);
    }
  }, [bestParamsByMode, bestParams, selectedBestParamsMode, selectedBestParamsStrategy]);

  const loadData = useCallback(async () => {
    const isInitialLoad = !initialLoadCompleteRef.current;
    try {
      setError(null);
      if (isInitialLoad) {
        setLoading(true);
      } else {
        setIsRefreshing(true);
      }
      // Lade Status zuerst (wichtigste Info), dann Rest parallel
      const statusResult = await Promise.allSettled([fetchAutoOptimizerStatus()]);
      const statusData = statusResult[0];
      
      // Lade Rest parallel (kann langsamer sein)
      const currentOffset = (resultsPage - 1) * RESULTS_PER_PAGE;
      const historyOffset = (historyPage - 1) * HISTORY_PER_PAGE;
      const [resultsData, historyData, bestParamsData, focusedData] = await Promise.allSettled([
        fetchAutoOptimizerResults(selectedStrategy || undefined, RESULTS_PER_PAGE, currentOffset),
        fetchAutoOptimizerHistory(HISTORY_PER_PAGE, historyOffset),
        fetchAutoOptimizerBestParams(),
        fetchFocusedStrategies().catch((err) => {
          // Wenn focused-strategies Endpoint nicht verfügbar ist, verwende leere Daten
          console.warn("Fehler beim Laden der fokussierten Strategien:", err);
          return {
            focused_strategies: [],
            available_strategies: [],
            strategies_with_params: [],
          };
        }),
      ]);
      
      if (statusData && statusData.status === "fulfilled") {
        setStatus(statusData.value);
      } else if (statusData && statusData.status === "rejected") {
        console.error("Fehler beim Laden des Status:", statusData.reason);
        // Setze Status auf null, damit die UI zeigt, dass der Optimizer nicht verfügbar ist
        setStatus(null);
        // Extrahiere die Fehlermeldung aus dem Reason
        const errorMessage = statusData.reason?.response?.data?.detail || 
                             statusData.reason?.message || 
                             String(statusData.reason);
        setError(`Auto-Optimizer nicht verfügbar: ${errorMessage}`);
      }
      
      if (resultsData && resultsData.status === "fulfilled") {
        setResults(resultsData.value.results);
        // Setze total_count - verwende API-Wert oder Fallback auf results.length
        const totalCount = resultsData.value.total_count ?? resultsData.value.results?.length ?? 0;
        setResultsTotalCount(totalCount);
      }
      if (historyData && historyData.status === "fulfilled") {
        setHistory(historyData.value.history);
        const totalHistory = historyData.value.total_count ?? historyData.value.history?.length ?? 0;
        setHistoryTotalCount(totalHistory);
      }
      if (bestParamsData && bestParamsData.status === "fulfilled") {
        setBestParams(bestParamsData.value.best_params);
        setBestParamsByMode(bestParamsData.value.best_params_by_mode ?? {});
      }
      if (focusedData && focusedData.status === "fulfilled") {
        setFocusedStrategiesData(focusedData.value);
        setSelectedFocusedStrategies(new Set(focusedData.value.focused_strategies || []));
      } else {
        // Fallback: leere Daten
        setFocusedStrategiesData({
          focused_strategies: [],
          available_strategies: [],
          strategies_with_params: [],
        });
      }
      
      // Sammle Fehler
      const errors: string[] = [];
      if (statusData && statusData.status === "rejected") errors.push(`Status: ${statusData.reason}`);
      if (resultsData && resultsData.status === "rejected") errors.push(`Results: ${resultsData.reason}`);
      if (historyData && historyData.status === "rejected") errors.push(`History: ${historyData.reason}`);
      if (bestParamsData && bestParamsData.status === "rejected") errors.push(`Best Params: ${bestParamsData.reason}`);
      
      if (errors.length > 0) {
        setError(`Fehler beim Laden: ${errors.join(", ")}`);
      }
      if (!initialLoadCompleteRef.current) {
        initialLoadCompleteRef.current = true;
        setInitialLoadComplete(true);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Fehler beim Laden der Daten");
    } finally {
      if (isInitialLoad) {
        setLoading(false);
      }
      setIsRefreshing(false);
    }
  }, [selectedStrategy, resultsPage, historyPage]);

  const loadHistoryPage = useCallback(async () => {
    if (!initialLoadComplete) return;
    try {
      setHistoryLoading(true);
      const historyOffset = (historyPage - 1) * HISTORY_PER_PAGE;
      const historyData = await fetchAutoOptimizerHistory(HISTORY_PER_PAGE, historyOffset);
      setHistory(historyData.history);
      const totalHistory = historyData.total_count ?? historyData.history?.length ?? 0;
      setHistoryTotalCount(totalHistory);
    } catch (err) {
      console.error("Fehler beim Laden der Historie:", err);
      setError(`Fehler beim Laden der Historie: ${err instanceof Error ? err.message : String(err)}`);
    } finally {
      setHistoryLoading(false);
    }
  }, [historyPage, initialLoadComplete]);

  // Lade nur Results neu, wenn sich resultsPage ändert (schneller als loadData)
  const loadResultsPage = useCallback(async () => {
    if (!initialLoadComplete) return;
    
    try {
      setResultsLoading(true);
      const currentOffset = (resultsPage - 1) * RESULTS_PER_PAGE;
      const resultsData = await fetchAutoOptimizerResults(
        selectedStrategy || undefined, 
        RESULTS_PER_PAGE, 
        currentOffset
      );
      
      setResults(resultsData.results);
      const totalCount = resultsData.total_count ?? resultsData.results?.length ?? 0;
      setResultsTotalCount(totalCount);
    } catch (err) {
      console.error("Fehler beim Laden der Results:", err);
      setError(`Fehler beim Laden der Ergebnisse: ${err instanceof Error ? err.message : String(err)}`);
    } finally {
      setResultsLoading(false);
    }
  }, [resultsPage, selectedStrategy, initialLoadComplete]);

  // Lade Results automatisch neu, wenn sich resultsPage ändert
  useEffect(() => {
    if (initialLoadComplete) {
      loadResultsPage();
    }
  }, [resultsPage, loadResultsPage]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (initialLoadComplete) {
      loadHistoryPage();
    }
  }, [historyPage, loadHistoryPage]); // eslint-disable-line react-hooks/exhaustive-deps

  const loadLogs = useCallback(async () => {
    // Prüfe ob wir am Ende scrollen sollten (im Log-Container)
    if (logContainerRef.current) {
      const container = logContainerRef.current;
      const isAtBottom = container.scrollHeight - container.scrollTop <= container.clientHeight + 10;
      shouldAutoScrollRef.current = isAtBottom;
    }
    
    try {
      setLogLoading(true);
      const logData = await fetchAutoOptimizerLog(200);
      const newLogs = logData.lines || [];
      setLogs(newLogs);
      
      // Auto-Scroll nur wenn wir bereits am Ende waren (im Log-Container)
      if (shouldAutoScrollRef.current && logContainerRef.current) {
        setTimeout(() => {
          if (logContainerRef.current) {
            logContainerRef.current.scrollTop = logContainerRef.current.scrollHeight;
          }
        }, 0);
      }
    } catch (err) {
      // Ignoriere Log-Fehler, um die Seite nicht zu blockieren
      console.error("Fehler beim Laden der Logs:", err);
    } finally {
      setLogLoading(false);
    }
  }, []);

  useEffect(() => {
    let mounted = true;
    
    const load = async () => {
      if (!mounted) return;
      await loadData();
      await loadLogs();
      // KEINE Scroll-Position-Wiederherstellung - React behält die Scroll-Position beim Re-Render
    };
    
    load();
    const interval = setInterval(load, 60000); // Refresh alle 60 Sekunden, weniger API-Last
    return () => {
      mounted = false;
      clearInterval(interval);
    };
  }, [loadData, loadLogs]);

  const handleStart = useCallback(async () => {
    try {
      setError(null);
      const result = await startAutoOptimizer();
      // Prüfe ob bereits läuft
      if (result.status === "already_running") {
        setError("Auto-Optimizer läuft bereits");
        await loadData();
        return;
      }
      if (result.status === "error") {
        setError(result.message || "Fehler beim Starten des Auto-Optimizers");
        await loadData();
        return;
      }
      
      // Warte kurz und lade dann den Status, um zu validieren, dass er wirklich läuft
      await new Promise(resolve => setTimeout(resolve, 1000));
      await loadData();
      
      // Prüfe nochmal, ob der Status wirklich "running" ist
      const verifyStatus = await fetchAutoOptimizerStatus();
      if (!verifyStatus.running) {
        setError("Auto-Optimizer wurde gestartet, aber läuft nicht. Bitte Backend-Logs prüfen.");
      } else {
        // Status ist korrekt - keine Fehlermeldung
        setError(null);
      }
    } catch (err: any) {
      const errorMessage = err?.response?.data?.detail || 
                          err?.message || 
                          (err instanceof Error ? err.message : "Fehler beim Starten");
      setError(`Fehler beim Starten: ${errorMessage}`);
      console.error("Fehler beim Starten des Auto-Optimizers:", err);
      // Lade Daten trotzdem, um aktuellen Status zu sehen
      await loadData();
    }
  }, [loadData]);

  const handleStop = useCallback(async () => {
    try {
      setError(null);
      await stopAutoOptimizer();
      await loadData();
    } catch (err: any) {
      const errorMessage = err?.response?.data?.detail || 
                          err?.message || 
                          (err instanceof Error ? err.message : "Fehler beim Stoppen");
      setError(`Fehler beim Stoppen: ${errorMessage}`);
      console.error("Fehler beim Stoppen des Auto-Optimizers:", err);
    }
  }, [loadData]);

  const handleSetFocusedStrategies = useCallback(async () => {
    try {
      setFocusedStrategiesLoading(true);
      setError(null);
      await setFocusedStrategies(Array.from(selectedFocusedStrategies));
      await loadData();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Fehler beim Setzen der fokussierten Strategien");
    } finally {
      setFocusedStrategiesLoading(false);
    }
  }, [selectedFocusedStrategies, loadData]);

  const handleToggleFocusedStrategy = useCallback((strategy: string) => {
    setSelectedFocusedStrategies((prev) => {
      const newSet = new Set(prev);
      if (newSet.has(strategy)) {
        newSet.delete(strategy);
      } else {
        newSet.add(strategy);
      }
      return newSet;
    });
  }, []);

  const strategies = Array.from(new Set([...results, ...history].map((r) => r.strategy_name))).sort();

  // Tabs für verschiedene Ansichten
  const tabs = [
    { id: "overview", label: "Übersicht" },
    { id: "branches", label: "Strategiezweige" },
    { id: "charts", label: "Charts" },
    { id: "llm-decisions", label: "LLM-Entscheidungen" },
    { id: "parameter-variations", label: "Parameter-Variations" },
    { id: "baseline-comparison", label: "Baseline-Vergleich" },
    { id: "process-flow", label: "Prozess-Flow" },
  ];

  // Lade Strategiezweig-Übersicht
  const loadBranchesOverview = useCallback(async () => {
    try {
      setBranchesLoading(true);
      const data = await fetchBranchesOverview();
      setBranchesOverview(data.branches);
    } catch (err) {
      console.error("Fehler beim Laden der Strategiezweig-Übersicht:", err);
      setError(`Fehler beim Laden der Strategiezweige: ${err instanceof Error ? err.message : String(err)}`);
    } finally {
      setBranchesLoading(false);
    }
  }, []);

  // Lade Branches-Übersicht wenn Tab aktiviert wird
  useEffect(() => {
    if (activeTab === "branches") {
      loadBranchesOverview();
    }
  }, [activeTab, loadBranchesOverview]);

  return (
    <div style={{ padding: "20px" }}>
      <h1>Auto Optimizer</h1>
      
      {/* Tabs */}
      <div style={{ marginBottom: "20px", borderBottom: "2px solid #ddd" }}>
        <div style={{ display: "flex", gap: "10px" }}>
          {tabs.map((tab) => (
            <button
              key={tab.id}
              onClick={() => setActiveTab(tab.id)}
              style={{
                padding: "10px 20px",
                backgroundColor: activeTab === tab.id ? "#007bff" : "#f5f5f5",
                color: activeTab === tab.id ? "#fff" : "#333",
                border: "none",
                borderRadius: "4px 4px 0 0",
                cursor: "pointer",
                borderBottom: activeTab === tab.id ? "2px solid #007bff" : "2px solid transparent",
              }}
            >
              {tab.label}
            </button>
          ))}
        </div>
      </div>
      
      {/* Strategie-Filter für alle Tabs */}
      <div style={{ marginBottom: "20px", padding: "10px", backgroundColor: "#f5f5f5", borderRadius: "4px" }}>
        <label>
          Strategie filtern:{" "}
          <select
            value={selectedStrategy}
            onChange={(e) => {
              setSelectedStrategy(e.target.value);
              setResultsPage(1); // Zurück zur ersten Seite beim Filterwechsel
              // loadData wird automatisch durch useEffect aufgerufen
            }}
            style={{ padding: "5px", marginLeft: "5px" }}
          >
            <option value="">Alle</option>
            {strategies.map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </select>
        </label>
      </div>

      {initialLoadComplete && isRefreshing && (
        <div style={{ marginBottom: "15px", display: "inline-flex", alignItems: "center", gap: "8px", fontSize: "0.9rem", color: "#666" }}>
          <span
            style={{
              width: "8px",
              height: "8px",
              borderRadius: "50%",
              backgroundColor: "#f5a623"
            }}
          />
          <span>Aktualisiere Daten...</span>
        </div>
      )}

      {error && (
        <div style={{ padding: "10px", marginBottom: "20px", backgroundColor: "#fee", color: "#c00", borderRadius: "4px" }}>
          {error}
        </div>
      )}

      {/* Tab Content */}
      {activeTab === "overview" && (
        <>
          {/* Status Section */}
          <div style={{ marginBottom: "30px", padding: "20px", backgroundColor: "#f5f5f5", borderRadius: "8px" }}>
            <h2>Status</h2>
            {status === null && !loading && (
              <div style={{ padding: "15px", backgroundColor: "#fff3cd", color: "#856404", borderRadius: "4px", marginBottom: "15px" }}>
                <strong>Warnung:</strong> Auto-Optimizer-Status konnte nicht geladen werden. 
                Möglicherweise ist der Auto-Optimizer nicht initialisiert oder der Backend-Service läuft nicht.
                <br />
                <button 
                  onClick={handleStart} 
                  style={{ 
                    marginTop: "10px", 
                    padding: "8px 16px", 
                    backgroundColor: "#0a0", 
                    color: "#fff", 
                    border: "none", 
                    borderRadius: "4px", 
                    cursor: "pointer" 
                  }}
                >
                  Versuche zu starten
                </button>
              </div>
            )}
            {status && (
              <div>
                <div style={{ marginBottom: "10px" }}>
                  <strong>Status:</strong>{" "}
                  <span style={{ color: status.running ? "#0a0" : "#a00" }}>
                    {status.running ? "Läuft" : "Gestoppt"}
                  </span>
                </div>
                
                {/* Optimierungs-Konfiguration */}
                <div style={{ marginBottom: "15px", padding: "10px", backgroundColor: "#e8f4f8", borderRadius: "4px", border: "1px solid #b3d9e6" }}>
                  <strong style={{ color: "#0066cc" }}>Optimierungs-Konfiguration:</strong>
                  <div style={{ marginTop: "8px", fontSize: "14px" }}>
                    <div style={{ marginBottom: "5px" }}>
                      <span style={{ fontWeight: "bold" }}>Backtest-Zeitraum:</span>{" "}
                      <span style={{ color: "#0066cc" }}>Letzte 6 Wochen (42 Tage)</span>
                      <span style={{ marginLeft: "10px", fontSize: "12px", color: "#666" }}>
                        (dynamisch, rollierend)
                      </span>
                    </div>
                    <div style={{ marginBottom: "5px" }}>
                      <span style={{ fontWeight: "bold" }}>Marktzeit-Filterung:</span>{" "}
                      <span style={{ color: "#0066cc" }}>7:00 - 23:00 ETC/EET</span>
                      <span style={{ marginLeft: "10px", fontSize: "12px", color: "#666" }}>
                        (nur relevante Handelszeiten)
                      </span>
                    </div>
                    <div style={{ marginBottom: "5px" }}>
                      <span style={{ fontWeight: "bold" }}>Strategie-Filter:</span>{" "}
                      <span style={{ color: "#0066cc" }}>Nur "core" Varianten</span>
                      <span style={{ marginLeft: "10px", fontSize: "12px", color: "#666" }}>
                        (z.B. vwap_momentum_core)
                      </span>
                    </div>
                    <div>
                      <span style={{ fontWeight: "bold" }}>Optimierungs-Modus:</span>{" "}
                      <span style={{ color: "#0066cc" }}>Pro Symbol × Strategie × Direction</span>
                      <span style={{ marginLeft: "10px", fontSize: "12px", color: "#666" }}>
                        (unabhängige Optimierung pro Zweig)
                      </span>
                    </div>
                  </div>
                </div>
                
                {status.current_test && (
                  <div style={{ marginBottom: "10px", padding: "10px", backgroundColor: "#fff", borderRadius: "4px" }}>
                    <strong>Aktueller Test:</strong>
                    <div>Strategie: {status.current_test.strategy}</div>
                    {status.current_test.symbol && (
                      <div>Symbol: <strong>{status.current_test.symbol}</strong></div>
                    )}
                    {status.current_test.direction && (
                      <div>Direction: <strong>{status.current_test.direction}</strong></div>
                    )}
                    {status.current_test.params && Array.isArray(status.current_test.params) && (
                      <div>Parameter: {status.current_test.params.join(", ")}</div>
                    )}
                    {status.current_test.combos !== undefined && (
                      <div>Kombinationen: {status.current_test.combos}</div>
                    )}
                    {status.current_test.mode && (
                      <div>Optimierungsmodus: <strong>{status.current_test.mode}</strong></div>
                    )}
                    {status.current_test.mode === "smart" && (
                      <>
                        {status.current_test.llm_model && (
                          <div style={{ marginTop: "5px", fontSize: "12px", color: "#666" }}>
                            LLM-Modell: <strong>{status.current_test.llm_model}</strong>
                          </div>
                        )}
                        {status.current_test.feedback_round !== undefined && status.current_test.feedback_round !== null && (
                          <div style={{ marginTop: "5px", fontSize: "12px", color: "#666" }}>
                            Feedback-Runde: <strong>{status.current_test.feedback_round}</strong>
                          </div>
                        )}
                        {status.current_test.llm_reasoning && (
                          <div style={{ marginTop: "10px", fontSize: "12px", color: "#666" }}>
                            <strong>LLM-Reasoning:</strong>
                            <div style={{ marginTop: "5px", padding: "5px", backgroundColor: "#f5f5f5", borderRadius: "4px", maxHeight: "100px", overflowY: "auto" }}>
                              {status.current_test.llm_reasoning.substring(0, 200)}
                              {status.current_test.llm_reasoning.length > 200 ? "..." : ""}
                            </div>
                          </div>
                        )}
                      </>
                    )}
                  </div>
                )}
                <div style={{ marginBottom: "10px" }}>
                  <strong>Tests abgeschlossen:</strong> {status.tests_completed}
                </div>
                <div style={{ marginBottom: "10px" }}>
                  <strong>Tests akzeptiert:</strong> {status.tests_accepted}
                </div>
                {status.last_update && (
                  <div style={{ marginBottom: "10px" }}>
                    <strong>Letzte Aktualisierung:</strong> {formatDate(status.last_update)}
                  </div>
                )}
                {status.debug && Object.keys(status.debug).length > 0 && (
                  <div style={{ marginBottom: "15px", padding: "10px", backgroundColor: "#e8f4f8", borderRadius: "4px", border: "1px solid #b3d9e6" }}>
                    <strong style={{ color: "#0066cc" }}>Debug-Informationen:</strong>
                    <div style={{ marginTop: "8px", fontSize: "14px" }}>
                      {status.debug.other_tests_running !== undefined && (
                        <div style={{ marginBottom: "5px" }}>
                          <span style={{ fontWeight: "bold" }}>Andere Tests laufen:</span>{" "}
                          <span style={{ color: status.debug.other_tests_running ? "#a00" : "#0a0" }}>
                            {status.debug.other_tests_running ? "Ja (Optimizer pausiert)" : "Nein"}
                          </span>
                        </div>
                      )}
                      {status.debug.available_cores !== undefined && (
                        <div style={{ marginBottom: "5px" }}>
                          <span style={{ fontWeight: "bold" }}>Verfügbare Cores:</span>{" "}
                          <span style={{ color: status.debug.available_cores > 0 ? "#0a0" : "#a00" }}>
                            {status.debug.available_cores}
                          </span>
                        </div>
                      )}
                      {status.debug.available_branches_count !== undefined && (
                        <div style={{ marginBottom: "5px" }}>
                          <span style={{ fontWeight: "bold" }}>Verfügbare Zweige:</span>{" "}
                          <span style={{ color: status.debug.available_branches_count > 0 ? "#0a0" : "#a00" }}>
                            {status.debug.available_branches_count}
                          </span>
                        </div>
                      )}
                      {status.debug.next_branch && (
                        <div style={{ marginBottom: "5px" }}>
                          <span style={{ fontWeight: "bold" }}>Nächster Zweig:</span>{" "}
                          <span style={{ color: "#0066cc" }}>{status.debug.next_branch}</span>
                        </div>
                      )}
                      {status.debug.optimizer_internal_running !== undefined && (
                        <div style={{ marginBottom: "5px" }}>
                          <span style={{ fontWeight: "bold" }}>Optimizer intern läuft:</span>{" "}
                          <span style={{ color: status.debug.optimizer_internal_running ? "#0a0" : "#a00" }}>
                            {status.debug.optimizer_internal_running ? "Ja" : "Nein"}
                          </span>
                        </div>
                      )}
                      {status.debug.branches_error && (
                        <div style={{ marginBottom: "5px", color: "#a00" }}>
                          <span style={{ fontWeight: "bold" }}>Fehler beim Laden der Zweige:</span> {status.debug.branches_error}
                        </div>
                      )}
                      {status.debug.debug_error && (
                        <div style={{ marginBottom: "5px", color: "#a00" }}>
                          <span style={{ fontWeight: "bold" }}>Debug-Fehler:</span> {status.debug.debug_error}
                        </div>
                      )}
                      {status.debug.paused_reason && (
                        <div style={{ marginBottom: "5px", padding: "8px", backgroundColor: "#fff3cd", color: "#856404", borderRadius: "4px" }}>
                          <span style={{ fontWeight: "bold" }}>⚠️ Pausiert:</span> {status.debug.paused_reason}
                        </div>
                      )}
                      {status.debug.timeout_warning && (
                        <div style={{ marginBottom: "5px", padding: "8px", backgroundColor: "#fff3cd", color: "#856404", borderRadius: "4px" }}>
                          <span style={{ fontWeight: "bold" }}>⏱️ Timeout-Warnung:</span> {status.debug.timeout_warning}
                          {status.debug.consecutive_timeouts !== undefined && (
                            <div style={{ marginTop: "3px", fontSize: "12px", opacity: 0.8 }}>
                              {status.debug.consecutive_timeouts} Timeouts in Folge
                            </div>
                          )}
                        </div>
                      )}
                      {status.debug.last_cycle_debug && (
                        <div style={{ marginBottom: "5px", padding: "8px", backgroundColor: "#fee", color: "#c00", borderRadius: "4px" }}>
                          <span style={{ fontWeight: "bold" }}>❌ Letzter Zyklus-Fehler:</span>
                          <div style={{ marginTop: "5px", fontSize: "13px" }}>
                            {status.debug.last_cycle_debug.error}
                            {status.debug.last_cycle_debug.available_branches !== undefined && (
                              <div style={{ marginTop: "3px", fontSize: "12px", opacity: 0.8 }}>
                                Verfügbare Zweige: {status.debug.last_cycle_debug.available_branches}
                              </div>
                            )}
                            {status.debug.last_cycle_debug.timestamp && (
                              <div style={{ marginTop: "3px", fontSize: "11px", opacity: 0.7 }}>
                                Zeit: {formatDate(status.debug.last_cycle_debug.timestamp)}
                              </div>
                            )}
                          </div>
                        </div>
                      )}
                    </div>
                  </div>
                )}
                {status.focused_strategies && status.focused_strategies.length > 0 && (
                  <div style={{ marginBottom: "10px", padding: "8px", backgroundColor: "#e8f4f8", borderRadius: "4px" }}>
                    <strong>Fokussierte Strategien:</strong> {status.focused_strategies.join(", ")}
                  </div>
                )}
                {(!status.focused_strategies || status.focused_strategies.length === 0) && (
                  <div style={{ marginBottom: "10px", padding: "8px", backgroundColor: "#fff3cd", borderRadius: "4px", color: "#856404" }}>
                    <strong>Hinweis:</strong> Keine fokussierten Strategien gesetzt - alle aktivierten Strategien werden optimiert
                  </div>
                )}
                {!status.running && (
                  <div style={{ marginBottom: "15px", padding: "10px", backgroundColor: "#fff3cd", color: "#856404", borderRadius: "4px" }}>
                    <strong>⚠️ Auto-Optimizer ist gestoppt</strong>
                    <div style={{ marginTop: "5px", fontSize: "14px" }}>
                      Klicken Sie auf "Starten", um den Auto-Optimizer zu aktivieren.
                      {status.last_update && (
                        <div style={{ marginTop: "5px", fontSize: "12px", color: "#666" }}>
                          Letzte Aktivität: {formatDate(status.last_update)}
                        </div>
                      )}
                    </div>
                  </div>
                )}
                <div style={{ marginTop: "15px" }}>
                  {status.running ? (
                    <button onClick={handleStop} style={{ padding: "8px 16px", backgroundColor: "#c00", color: "#fff", border: "none", borderRadius: "4px", cursor: "pointer" }}>
                      Stoppen
                    </button>
                  ) : (
                    <button onClick={handleStart} style={{ padding: "8px 16px", backgroundColor: "#0a0", color: "#fff", border: "none", borderRadius: "4px", cursor: "pointer" }}>
                      Starten
                    </button>
                  )}
                </div>
              </div>
            )}
          </div>

          {/* Strategie-Fokus Section */}
          <div style={{ marginBottom: "30px", padding: "20px", backgroundColor: "#f5f5f5", borderRadius: "8px" }}>
            <h2>Strategie-Fokus</h2>
            <p style={{ marginBottom: "15px", color: "#666" }}>
              Wähle die Strategien aus, die optimiert werden sollen. Wenn keine Strategien ausgewählt sind, werden alle aktivierten Strategien optimiert.
            </p>
            {focusedStrategiesData && (
              <>
                <div style={{ marginBottom: "15px" }}>
                  {focusedStrategiesData.available_strategies.length > 0 ? (
                    Array.from(new Set(focusedStrategiesData.available_strategies)).map((strategy) => (
                      <label
                        key={strategy}
                        style={{
                          display: "block",
                          marginBottom: "8px",
                          padding: "8px",
                          backgroundColor: selectedFocusedStrategies.has(strategy) ? "#e8f4f8" : "#fff",
                          borderRadius: "4px",
                          cursor: "pointer",
                          border: selectedFocusedStrategies.has(strategy) ? "2px solid #0a0" : "2px solid #ddd",
                        }}
                      >
                        <input
                          type="checkbox"
                          checked={selectedFocusedStrategies.has(strategy)}
                          onChange={() => handleToggleFocusedStrategy(strategy)}
                          style={{ marginRight: "8px" }}
                        />
                        <strong>{strategy}</strong>
                        {!focusedStrategiesData.strategies_with_params.includes(strategy) && (
                          <span style={{ marginLeft: "8px", color: "#999", fontSize: "12px" }}>
                            (keine Parameter-Definition)
                          </span>
                        )}
                      </label>
                    ))
                  ) : (
                    <div style={{ padding: "10px", backgroundColor: "#fff", borderRadius: "4px", color: "#666" }}>
                      Keine aktivierten Strategien gefunden
                    </div>
                  )}
                </div>
                <div>
                  <button
                    onClick={handleSetFocusedStrategies}
                    disabled={focusedStrategiesLoading}
                    style={{
                      padding: "8px 16px",
                      backgroundColor: focusedStrategiesLoading ? "#ccc" : "#0066cc",
                      color: "#fff",
                      border: "none",
                      borderRadius: "4px",
                      cursor: focusedStrategiesLoading ? "not-allowed" : "pointer",
                    }}
                  >
                    {focusedStrategiesLoading ? "Speichere..." : "Fokus setzen"}
                  </button>
                  <button
                    onClick={async () => {
                      setSelectedFocusedStrategies(new Set());
                      try {
                        setFocusedStrategiesLoading(true);
                        setError(null);
                        await setFocusedStrategies([]);
                        await loadData();
                      } catch (err) {
                        setError(err instanceof Error ? err.message : "Fehler beim Zurücksetzen");
                      } finally {
                        setFocusedStrategiesLoading(false);
                      }
                    }}
                    disabled={focusedStrategiesLoading}
                    style={{
                      marginLeft: "10px",
                      padding: "8px 16px",
                      backgroundColor: focusedStrategiesLoading ? "#ccc" : "#666",
                      color: "#fff",
                      border: "none",
                      borderRadius: "4px",
                      cursor: focusedStrategiesLoading ? "not-allowed" : "pointer",
                    }}
                  >
                    Fokus zurücksetzen (alle verwenden)
                  </button>
                </div>
                {selectedFocusedStrategies.size > 0 && (
                  <div style={{ marginTop: "15px", padding: "10px", backgroundColor: "#fff", borderRadius: "4px" }}>
                    <strong>Ausgewählt:</strong> {Array.from(selectedFocusedStrategies).join(", ")}
                  </div>
                )}
              </>
            )}
          </div>

          {/* Best Parameters Section */}
          {(Object.keys(bestParams).length > 0 || Object.keys(bestParamsByMode).length > 0) && (
            <div style={{ marginBottom: "30px", padding: "20px", backgroundColor: "#f5f5f5", borderRadius: "8px" }}>
              <div style={{ display: "flex", alignItems: "center", gap: "12px", marginBottom: "15px" }}>
                <h2 style={{ margin: 0 }}>Beste Parameter</h2>
                <div style={{ display: "flex", alignItems: "center", gap: "8px" }}>
                  {Object.keys(bestParamsByMode).length > 0 && (
                    <>
                      <label htmlFor="best-params-mode" style={{ fontSize: "12px", color: "#444" }}>
                        Typ:
                      </label>
                      <select
                        id="best-params-mode"
                        value={selectedBestParamsMode}
                        onChange={(event) => setSelectedBestParamsMode(event.target.value)}
                        style={{ padding: "6px 10px", borderRadius: "4px", border: "1px solid #ccc", fontSize: "12px" }}
                      >
                        {Object.keys(bestParamsByMode).map((mode) => (
                          <option key={mode} value={mode}>
                            {mode}
                          </option>
                        ))}
                      </select>
                    </>
                  )}
                  <label htmlFor="best-params-strategy" style={{ fontSize: "12px", color: "#444" }}>
                    Strategie:
                  </label>
                  <select
                    id="best-params-strategy"
                    value={selectedBestParamsStrategy}
                    onChange={(event) => setSelectedBestParamsStrategy(event.target.value)}
                    style={{ padding: "6px 10px", borderRadius: "4px", border: "1px solid #ccc", fontSize: "12px" }}
                  >
                    {Object.keys(
                      Object.keys(bestParamsByMode).length > 0
                        ? (bestParamsByMode[selectedBestParamsMode] || bestParamsByMode[Object.keys(bestParamsByMode)[0]] || {})
                        : bestParams
                    ).map((strategy) => (
                      <option key={strategy} value={strategy}>
                        {strategy}
                      </option>
                    ))}
                  </select>
                </div>
              </div>
              {selectedBestParamsStrategy &&
              (Object.keys(bestParamsByMode).length > 0
                ? (bestParamsByMode[selectedBestParamsMode] || bestParamsByMode[Object.keys(bestParamsByMode)[0]] || {})[
                    selectedBestParamsStrategy
                  ]
                : bestParams[selectedBestParamsStrategy]) ? (
                <div style={{ padding: "10px", backgroundColor: "#fff", borderRadius: "4px" }}>
                  <strong>{selectedBestParamsStrategy}:</strong>
                  <pre style={{ marginTop: "5px", fontSize: "12px", overflow: "auto" }}>
                    {JSON.stringify(
                      Object.keys(bestParamsByMode).length > 0
                        ? (bestParamsByMode[selectedBestParamsMode] || bestParamsByMode[Object.keys(bestParamsByMode)[0]] || {})[
                            selectedBestParamsStrategy
                          ]
                        : bestParams[selectedBestParamsStrategy],
                      null,
                      2
                    )}
                  </pre>
                </div>
              ) : (
                <div style={{ padding: "10px", backgroundColor: "#fff", borderRadius: "4px", color: "#666" }}>
                  Keine Strategie ausgewählt.
                </div>
              )}
            </div>
          )}

          {/* Results Section */}
          <div style={{ marginBottom: "30px" }}>
            <h2>Top Ergebnisse</h2>
            {loading ? (
              <div>Lädt...</div>
            ) : resultsLoading ? (
              <div style={{ padding: "20px", textAlign: "center", color: "#666" }}>
                Lade Seite {resultsPage}...
              </div>
            ) : results.length === 0 ? (
              <div>Keine Ergebnisse gefunden</div>
            ) : (
              <div style={{ overflowX: "auto" }}>
                <table style={{ width: "100%", borderCollapse: "collapse", minWidth: "1400px" }}>
                  <thead>
                    <tr style={{ backgroundColor: "#f0f0f0" }}>
                      <th style={{ padding: "8px", textAlign: "left", border: "1px solid #ddd" }}>Strategie</th>
                      <th style={{ padding: "8px", textAlign: "left", border: "1px solid #ddd" }}>Trades</th>
                      <th style={{ padding: "8px", textAlign: "right", border: "1px solid #ddd" }}>PNL</th>
                      <th style={{ padding: "8px", textAlign: "right", border: "1px solid #ddd" }}>PNL/Trade</th>
                      <th style={{ padding: "8px", textAlign: "right", border: "1px solid #ddd" }}>Winrate</th>
                      <th style={{ padding: "8px", textAlign: "right", border: "1px solid #ddd" }}>Score</th>
                      <th style={{ padding: "8px", textAlign: "right", border: "1px solid #ddd" }}>Max DD</th>
                      <th style={{ padding: "8px", textAlign: "right", border: "1px solid #ddd" }}>Profit Factor</th>
                      <th style={{ padding: "8px", textAlign: "left", border: "1px solid #ddd" }}>Modus</th>
                      <th style={{ padding: "8px", textAlign: "left", border: "1px solid #ddd" }}>LLM-Modell</th>
                      <th style={{ padding: "8px", textAlign: "left", border: "1px solid #ddd" }}>Feedback</th>
                      <th style={{ padding: "8px", textAlign: "left", border: "1px solid #ddd" }}>Akzeptiert</th>
                      <th style={{ padding: "8px", textAlign: "left", border: "1px solid #ddd" }}>Zeitraum</th>
                      <th style={{ padding: "8px", textAlign: "left", border: "1px solid #ddd" }}>Parameter</th>
                    </tr>
                  </thead>
                  <tbody>
                    {results.map((result) => (
                      <tr key={result.test_id} style={{ backgroundColor: result.accepted ? "#e8f5e9" : result.optimization_mode === "smart" ? "#fff3cd" : "#fff" }}>
                        <td style={{ padding: "8px", border: "1px solid #ddd" }}>{result.strategy_name}</td>
                        <td style={{ padding: "8px", border: "1px solid #ddd" }}>{result.trades}</td>
                        <td style={{ padding: "8px", border: "1px solid #ddd", textAlign: "right" }}>
                          {formatNumber(result.pnl)}
                        </td>
                        <td style={{ padding: "8px", border: "1px solid #ddd", textAlign: "right" }}>
                          {formatNumber(result.pnl_per_trade, 4)}
                        </td>
                        <td style={{ padding: "8px", border: "1px solid #ddd", textAlign: "right" }}>
                          {formatPercent(result.winrate)}
                        </td>
                        <td style={{ padding: "8px", border: "1px solid #ddd", textAlign: "right" }}>
                          {result.score !== null && result.score !== undefined ? formatNumber(result.score, 4) : "—"}
                        </td>
                        <td style={{ padding: "8px", border: "1px solid #ddd", textAlign: "right" }}>
                          {result.max_drawdown !== null && result.max_drawdown !== undefined ? formatNumber(result.max_drawdown, 2) : "—"}
                        </td>
                        <td style={{ padding: "8px", border: "1px solid #ddd", textAlign: "right" }}>
                          {result.profit_factor !== null && result.profit_factor !== undefined ? formatNumber(result.profit_factor, 2) : "—"}
                        </td>
                        <td style={{ padding: "8px", border: "1px solid #ddd", fontSize: "12px" }}>
                          {result.optimization_mode || "—"}
                        </td>
                        <td style={{ padding: "8px", border: "1px solid #ddd", fontSize: "12px" }}>
                          {result.llm_model || "—"}
                        </td>
                        <td style={{ padding: "8px", border: "1px solid #ddd", fontSize: "12px" }}>
                          {result.feedback_round !== null && result.feedback_round !== undefined ? `Runde ${result.feedback_round}` : "—"}
                        </td>
                        <td style={{ padding: "8px", border: "1px solid #ddd" }}>
                          {result.accepted ? "✓" : "—"}
                        </td>
                        <td style={{ padding: "8px", border: "1px solid #ddd", fontSize: "12px" }}>
                          {result.test_date_range}
                        </td>
                        <td style={{ padding: "8px", border: "1px solid #ddd", fontSize: "11px" }}>
                          <details>
                            <summary>Parameter</summary>
                            <pre style={{ marginTop: "5px", fontSize: "10px", maxHeight: "200px", overflow: "auto" }}>
                              {JSON.stringify(result.param_values, null, 2)}
                            </pre>
                          </details>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
            {/* Paginierung */}
            {(resultsTotalCount > RESULTS_PER_PAGE || results.length > RESULTS_PER_PAGE) && (
              <div style={{ marginTop: "15px", display: "flex", alignItems: "center", justifyContent: "center", gap: "10px" }}>
                <button
                  onClick={() => {
                    if (resultsPage > 1) {
                      setResultsPage(resultsPage - 1);
                    }
                  }}
                  disabled={resultsPage === 1}
                  style={{
                    padding: "6px 12px",
                    backgroundColor: (resultsPage === 1 || resultsLoading) ? "#ccc" : "#0066cc",
                    color: "#fff",
                    border: "none",
                    borderRadius: "4px",
                    cursor: resultsPage === 1 ? "not-allowed" : "pointer",
                  }}
                >
                  ← Zurück
                </button>
                <span style={{ padding: "0 10px" }}>
                  Seite {resultsPage} von {Math.ceil((resultsTotalCount || results.length) / RESULTS_PER_PAGE)} 
                  ({(resultsTotalCount || results.length)} Ergebnisse insgesamt)
                </span>
                <button
                  onClick={() => {
                    const maxPages = Math.ceil((resultsTotalCount || results.length) / RESULTS_PER_PAGE);
                    if (resultsPage < maxPages && !resultsLoading) {
                      setResultsPage(resultsPage + 1);
                    }
                  }}
                  disabled={resultsPage >= Math.ceil((resultsTotalCount || results.length) / RESULTS_PER_PAGE) || resultsLoading}
                  style={{
                    padding: "6px 12px",
                    backgroundColor: (resultsPage >= Math.ceil((resultsTotalCount || results.length) / RESULTS_PER_PAGE) || resultsLoading) ? "#ccc" : "#0066cc",
                    color: "#fff",
                    border: "none",
                    borderRadius: "4px",
                    cursor: resultsPage >= Math.ceil((resultsTotalCount || results.length) / RESULTS_PER_PAGE) ? "not-allowed" : "pointer",
                  }}
                >
                  Weiter →
                </button>
              </div>
            )}
          </div>

          {/* History Section */}
          <div style={{ marginBottom: "30px" }}>
            <h2>Historie</h2>
            {loading ? (
              <div>Lädt...</div>
            ) : historyLoading ? (
              <div style={{ padding: "20px", textAlign: "center", color: "#666" }}>
                Lade Seite {historyPage}...
              </div>
            ) : history.length === 0 ? (
              <div>Keine Historie gefunden</div>
            ) : (
              <div style={{ overflowX: "auto" }}>
                <table style={{ width: "100%", borderCollapse: "collapse", minWidth: "1100px" }}>
                  <thead>
                    <tr style={{ backgroundColor: "#f0f0f0" }}>
                      <th style={{ padding: "8px", textAlign: "left", border: "1px solid #ddd" }}>Zeit</th>
                      <th style={{ padding: "8px", textAlign: "left", border: "1px solid #ddd" }}>Strategie</th>
                      <th style={{ padding: "8px", textAlign: "right", border: "1px solid #ddd" }}>PNL</th>
                      <th style={{ padding: "8px", textAlign: "right", border: "1px solid #ddd" }}>PNL/Trade</th>
                      <th style={{ padding: "8px", textAlign: "right", border: "1px solid #ddd" }}>Trades</th>
                      <th style={{ padding: "8px", textAlign: "right", border: "1px solid #ddd" }}>Winrate</th>
                      <th style={{ padding: "8px", textAlign: "right", border: "1px solid #ddd" }}>Score</th>
                      <th style={{ padding: "8px", textAlign: "right", border: "1px solid #ddd" }}>Max DD</th>
                      <th style={{ padding: "8px", textAlign: "right", border: "1px solid #ddd" }}>Profit Factor</th>
                      <th style={{ padding: "8px", textAlign: "left", border: "1px solid #ddd" }}>Modus</th>
                      <th style={{ padding: "8px", textAlign: "left", border: "1px solid #ddd" }}>LLM-Modell</th>
                      <th style={{ padding: "8px", textAlign: "left", border: "1px solid #ddd" }}>Feedback</th>
                      <th style={{ padding: "8px", textAlign: "left", border: "1px solid #ddd" }}>Akzeptiert</th>
                    </tr>
                  </thead>
                  <tbody>
                    {history.map((result) => (
                      <tr key={result.test_id} style={{ backgroundColor: result.accepted ? "#e8f5e9" : result.optimization_mode === "smart" ? "#fff3cd" : "#fff" }}>
                        <td style={{ padding: "8px", border: "1px solid #ddd", fontSize: "12px" }}>
                          {formatDate(result.timestamp)}
                        </td>
                        <td style={{ padding: "8px", border: "1px solid #ddd" }}>{result.strategy_name}</td>
                        <td style={{ padding: "8px", border: "1px solid #ddd", textAlign: "right" }}>
                          {formatNumber(result.pnl)}
                        </td>
                        <td style={{ padding: "8px", border: "1px solid #ddd", textAlign: "right" }}>
                          {formatNumber(result.pnl_per_trade, 4)}
                        </td>
                        <td style={{ padding: "8px", border: "1px solid #ddd", textAlign: "right" }}>
                          {result.trades}
                        </td>
                        <td style={{ padding: "8px", border: "1px solid #ddd", textAlign: "right" }}>
                          {formatPercent(result.winrate)}
                        </td>
                        <td style={{ padding: "8px", border: "1px solid #ddd", textAlign: "right" }}>
                          {result.score !== null && result.score !== undefined ? formatNumber(result.score, 4) : "—"}
                        </td>
                        <td style={{ padding: "8px", border: "1px solid #ddd", textAlign: "right" }}>
                          {result.max_drawdown !== null && result.max_drawdown !== undefined ? formatNumber(result.max_drawdown, 2) : "—"}
                        </td>
                        <td style={{ padding: "8px", border: "1px solid #ddd", textAlign: "right" }}>
                          {result.profit_factor !== null && result.profit_factor !== undefined ? formatNumber(result.profit_factor, 2) : "—"}
                        </td>
                        <td style={{ padding: "8px", border: "1px solid #ddd", fontSize: "12px" }}>
                          {result.optimization_mode || "—"}
                        </td>
                        <td style={{ padding: "8px", border: "1px solid #ddd", fontSize: "12px" }}>
                          {result.llm_model || "—"}
                        </td>
                        <td style={{ padding: "8px", border: "1px solid #ddd", fontSize: "12px" }}>
                          {result.feedback_round !== null && result.feedback_round !== undefined ? `Runde ${result.feedback_round}` : "—"}
                        </td>
                        <td style={{ padding: "8px", border: "1px solid #ddd" }}>
                          {result.accepted ? "✓" : "—"}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
            {/* Paginierung */}
            {(historyTotalCount > HISTORY_PER_PAGE || history.length > HISTORY_PER_PAGE) && (
              <div style={{ marginTop: "15px", display: "flex", alignItems: "center", justifyContent: "center", gap: "10px" }}>
                <button
                  onClick={() => {
                    if (historyPage > 1) {
                      setHistoryPage(historyPage - 1);
                    }
                  }}
                  disabled={historyPage === 1}
                  style={{
                    padding: "6px 12px",
                    backgroundColor: (historyPage === 1 || historyLoading) ? "#ccc" : "#0066cc",
                    color: "#fff",
                    border: "none",
                    borderRadius: "4px",
                    cursor: historyPage === 1 ? "not-allowed" : "pointer",
                  }}
                >
                  ← Zurück
                </button>
                <span style={{ padding: "0 10px" }}>
                  Seite {historyPage} von {Math.ceil((historyTotalCount || history.length) / HISTORY_PER_PAGE)}
                  ({(historyTotalCount || history.length)} Einträge insgesamt)
                </span>
                <button
                  onClick={() => {
                    const maxPages = Math.ceil((historyTotalCount || history.length) / HISTORY_PER_PAGE);
                    if (historyPage < maxPages && !historyLoading) {
                      setHistoryPage(historyPage + 1);
                    }
                  }}
                  disabled={historyPage >= Math.ceil((historyTotalCount || history.length) / HISTORY_PER_PAGE) || historyLoading}
                  style={{
                    padding: "6px 12px",
                    backgroundColor: (historyPage >= Math.ceil((historyTotalCount || history.length) / HISTORY_PER_PAGE) || historyLoading) ? "#ccc" : "#0066cc",
                    color: "#fff",
                    border: "none",
                    borderRadius: "4px",
                    cursor: historyPage >= Math.ceil((historyTotalCount || history.length) / HISTORY_PER_PAGE) ? "not-allowed" : "pointer",
                  }}
                >
                  Weiter →
                </button>
              </div>
            )}
          </div>

          {/* Live Log Section */}
          <div style={{ marginBottom: "30px" }}>
            <div style={{ padding: "20px", backgroundColor: "#f5f5f5", borderRadius: "8px" }}>
              <h2>Live Log</h2>
              {logLoading ? (
                <p>Lade Logs...</p>
              ) : logs.length === 0 ? (
                <p>Keine Logs verfügbar</p>
              ) : (
                <pre
                  ref={logContainerRef}
                  className="log-viewer"
                  onScroll={() => {
                    // Prüfe ob Benutzer manuell scrollt
                    if (logContainerRef.current) {
                      const container = logContainerRef.current;
                      const isAtBottom = container.scrollHeight - container.scrollTop <= container.clientHeight + 10;
                      shouldAutoScrollRef.current = isAtBottom;
                    }
                  }}
                >
                  {logs.map((line, idx) => (
                    <span key={idx}>{line}</span>
                  ))}
                </pre>
              )}
            </div>
          </div>
        </>
      )}

      {/* Strategiezweige Tab */}
      {activeTab === "branches" && (
        <div style={{ marginBottom: "30px" }}>
          <h2>Strategiezweig-Übersicht</h2>
          <p style={{ marginBottom: "15px", color: "#666" }}>
            Übersicht aller optimierten Strategiezweige (Strategie × Symbol × Direction) mit aktuell besten Settings und Ergebnissen.
          </p>
          
          {branchesLoading ? (
            <div style={{ padding: "20px", textAlign: "center", color: "#666" }}>
              Lade Strategiezweige...
            </div>
          ) : branchesOverview.length === 0 ? (
            <div style={{ padding: "20px", textAlign: "center", color: "#666" }}>
              Keine optimierten Strategiezweige gefunden.
            </div>
          ) : (
            <div style={{ overflowX: "auto" }}>
              <table style={{ width: "100%", borderCollapse: "collapse", minWidth: "1600px" }}>
                <thead>
                  <tr style={{ backgroundColor: "#f0f0f0" }}>
                    <th style={{ padding: "8px", textAlign: "left", border: "1px solid #ddd" }}>Strategie</th>
                    <th style={{ padding: "8px", textAlign: "left", border: "1px solid #ddd" }}>Symbol</th>
                    <th style={{ padding: "8px", textAlign: "left", border: "1px solid #ddd" }}>Direction</th>
                    <th style={{ padding: "8px", textAlign: "right", border: "1px solid #ddd" }}>PNL</th>
                    <th style={{ padding: "8px", textAlign: "right", border: "1px solid #ddd" }}>Trades</th>
                    <th style={{ padding: "8px", textAlign: "right", border: "1px solid #ddd" }}>PNL/Trade</th>
                    <th style={{ padding: "8px", textAlign: "right", border: "1px solid #ddd" }}>Winrate</th>
                    <th style={{ padding: "8px", textAlign: "right", border: "1px solid #ddd" }}>Score</th>
                    <th style={{ padding: "8px", textAlign: "right", border: "1px solid #ddd" }}>Max DD</th>
                    <th style={{ padding: "8px", textAlign: "right", border: "1px solid #ddd" }}>Profit Factor</th>
                    <th style={{ padding: "8px", textAlign: "left", border: "1px solid #ddd" }}>Zeitraum</th>
                    <th style={{ padding: "8px", textAlign: "left", border: "1px solid #ddd" }}>Letzte Änderung</th>
                    <th style={{ padding: "8px", textAlign: "left", border: "1px solid #ddd" }}>Settings</th>
                    <th style={{ padding: "8px", textAlign: "left", border: "1px solid #ddd" }}>Kommentar</th>
                  </tr>
                </thead>
                <tbody>
                  {branchesOverview.map((branch, idx) => (
                    <tr key={`${branch.strategy_name}-${branch.symbol || 'ALL'}-${branch.direction || 'BOTH'}-${idx}`} 
                        style={{ backgroundColor: idx % 2 === 0 ? "#fff" : "#f9f9f9" }}>
                      <td style={{ padding: "8px", border: "1px solid #ddd", fontWeight: "bold" }}>
                        {branch.strategy_name}
                      </td>
                      <td style={{ padding: "8px", border: "1px solid #ddd" }}>
                        {branch.symbol || "ALL"}
                      </td>
                      <td style={{ padding: "8px", border: "1px solid #ddd" }}>
                        {branch.direction || "BOTH"}
                      </td>
                      <td style={{ padding: "8px", border: "1px solid #ddd", textAlign: "right" }}>
                        {formatNumber(branch.metrics.pnl)}
                      </td>
                      <td style={{ padding: "8px", border: "1px solid #ddd", textAlign: "right" }}>
                        {branch.metrics.trades}
                      </td>
                      <td style={{ padding: "8px", border: "1px solid #ddd", textAlign: "right" }}>
                        {formatNumber(branch.metrics.pnl_per_trade, 4)}
                      </td>
                      <td style={{ padding: "8px", border: "1px solid #ddd", textAlign: "right" }}>
                        {formatPercent(branch.metrics.winrate)}
                      </td>
                      <td style={{ padding: "8px", border: "1px solid #ddd", textAlign: "right" }}>
                        {branch.metrics.score !== null ? formatNumber(branch.metrics.score, 4) : "—"}
                      </td>
                      <td style={{ padding: "8px", border: "1px solid #ddd", textAlign: "right" }}>
                        {branch.metrics.max_drawdown !== null ? formatNumber(branch.metrics.max_drawdown, 2) : "—"}
                      </td>
                      <td style={{ padding: "8px", border: "1px solid #ddd", textAlign: "right" }}>
                        {branch.metrics.profit_factor !== null ? formatNumber(branch.metrics.profit_factor, 2) : "—"}
                      </td>
                      <td style={{ padding: "8px", border: "1px solid #ddd", fontSize: "12px" }}>
                        {branch.test_date_range}
                      </td>
                      <td style={{ padding: "8px", border: "1px solid #ddd", fontSize: "12px" }}>
                        {formatDate(branch.last_update)}
                      </td>
                      <td style={{ padding: "8px", border: "1px solid #ddd", fontSize: "11px" }}>
                        <details>
                          <summary style={{ cursor: "pointer", color: "#0066cc" }}>Parameter anzeigen</summary>
                          <pre style={{ marginTop: "5px", fontSize: "10px", maxHeight: "200px", overflow: "auto", backgroundColor: "#f5f5f5", padding: "8px", borderRadius: "4px" }}>
                            {JSON.stringify(branch.params, null, 2)}
                          </pre>
                        </details>
                      </td>
                      <td style={{ padding: "8px", border: "1px solid #ddd", fontSize: "11px", maxWidth: "300px" }}>
                        {branch.llm_reasoning ? (
                          <details>
                            <summary style={{ cursor: "pointer", color: "#0066cc" }}>LLM-Kommentar</summary>
                            <div style={{ marginTop: "5px", padding: "8px", backgroundColor: "#e8f4f8", borderRadius: "4px", fontSize: "10px", lineHeight: "1.4", maxHeight: "200px", overflow: "auto" }}>
                              {branch.llm_reasoning}
                            </div>
                          </details>
                        ) : (
                          <span style={{ color: "#999" }}>—</span>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}

      {activeTab === "charts" && (
        <div style={{ marginBottom: "30px" }}>
          <div style={{ marginBottom: "30px" }}>
            <ScoreTimelineChart strategyName={selectedStrategy || undefined} limit={100} />
          </div>
          <div style={{ marginBottom: "30px" }}>
            <PnLTradeChart strategyName={selectedStrategy || undefined} limit={100} />
          </div>
          <div style={{ marginBottom: "30px" }}>
            <WinrateChart strategyName={selectedStrategy || undefined} limit={100} />
          </div>
        </div>
      )}

      {activeTab === "llm-decisions" && (
        <div style={{ marginBottom: "30px" }}>
          <LLMDecisionsTimeline strategyName={selectedStrategy || undefined} limit={50} />
        </div>
      )}

      {activeTab === "parameter-variations" && (
        <div style={{ marginBottom: "30px" }}>
          <ParameterVariationsChart strategyName={selectedStrategy || undefined} />
        </div>
      )}

      {activeTab === "baseline-comparison" && (
        <div style={{ marginBottom: "30px" }}>
          <BaselineComparisonChart strategyName={selectedStrategy || undefined} />
        </div>
      )}

      {activeTab === "process-flow" && (
        <div style={{ marginBottom: "30px" }}>
          <OptimizationProcessFlow strategyName={selectedStrategy || undefined} limit={50} />
        </div>
      )}

    </div>
  );
}
