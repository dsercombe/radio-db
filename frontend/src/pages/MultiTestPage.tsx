import { FormEvent, useCallback, useEffect, useMemo, useState } from "react";
import {
  listMultiTests,
  startMultiTest,
  cancelMultiTest,
  fetchMultiTest,
  fetchMultiTestLog,
  fetchMultiTestConfig,
  fetchMultiTestPresets,
  upsertMultiTestPreset,
  deleteMultiTestPreset,
  estimateMultiTest,
  fetchMultiTestParamGrid,
  updateMultiTestParamGrid,
  resetMultiTestParamGrid,
  fetchTemplates,
  fetchTemplate,
  fetchCurrentSettings,
  fetchSettingsSchema,
  type MultiTestMeta,
  type MultiTestResult,
  type MultiTestPreset,
  type MultiTestParamGrid,
  type TemplateMeta
} from "../api/client";
import { extractEditableFields, getNestedValue } from "../utils/settings";

const CURRENT_TEMPLATE_SENTINEL = "__current__";

type ComboEstimate = {
  total: number | null;
  strategies: Array<{
    name: string;
    alias?: string | null;
    base: number;
    effective: number;
    truncated: boolean;
  }>;
};

function formatDateInput(date: Date): string {
  return date.toISOString().slice(0, 10);
}

function formatDate(value?: string | null): string {
  if (!value) {
    return "—";
  }
  try {
    return new Date(value).toLocaleString();
  } catch {
    return value;
  }
}

function formatNumber(value: number | null | undefined, digits = 2): string {
  if (value == null || Number.isNaN(value)) {
    return "—";
  }
  return value.toFixed(digits);
}

function groupResultsByStrategy(results: MultiTestResult[]): Record<string, MultiTestResult[]> {
  return results.reduce((acc, result) => {
    const list = acc[result.strategy_name] ?? [];
    list.push(result);
    acc[result.strategy_name] = list;
    return acc;
  }, {} as Record<string, MultiTestResult[]>);
}

export function MultiTestPage(): JSX.Element {
  const [templates, setTemplates] = useState<TemplateMeta[]>([]);
  const [tooltips, setTooltips] = useState<Record<string, string>>({});
  const [selectedTemplate, setSelectedTemplate] = useState<string>(CURRENT_TEMPLATE_SENTINEL);
  const [settingsPreview, setSettingsPreview] = useState<Record<string, unknown> | null>(null);
  const [settingsFields, setSettingsFields] = useState<ReturnType<typeof extractEditableFields>>([]);
  const [presets, setPresets] = useState<MultiTestPreset[]>([]);
  const [selectedPresetId, setSelectedPresetId] = useState<string | null>(null);
  const [presetMessage, setPresetMessage] = useState<string | null>(null);
  const [presetError, setPresetError] = useState<string | null>(null);

  const [multiTests, setMultiTests] = useState<MultiTestMeta[]>([]);
  const [activeTest, setActiveTest] = useState<MultiTestMeta | null>(null);
  const [listLoading, setListLoading] = useState<boolean>(false);

  const [startDate, setStartDate] = useState<string>(() => {
    const date = new Date();
    date.setDate(date.getDate() - 60);
    return formatDateInput(date);
  });
  const [endDate, setEndDate] = useState<string>(() => formatDateInput(new Date()));
  const [strategyName, setStrategyName] = useState<string>("");
  const [sampleRatio, setSampleRatio] = useState<number>(0.1);
  const [maxCombos, setMaxCombos] = useState<number | "">("");
  const [comboTimeout, setComboTimeout] = useState<number>(300);
  const [instrumentTimeout, setInstrumentTimeout] = useState<number>(120);
  const [notes, setNotes] = useState<string>("");
  const [startLoading, setStartLoading] = useState<boolean>(false);
  const [startError, setStartError] = useState<string | null>(null);
  const [startSuccess, setStartSuccess] = useState<string | null>(null);

  const [selectedTestId, setSelectedTestId] = useState<string | null>(null);
  const [detailLoading, setDetailLoading] = useState<boolean>(false);
  const [detailError, setDetailError] = useState<string | null>(null);
  const [detailMeta, setDetailMeta] = useState<MultiTestMeta | null>(null);
  const [detailResults, setDetailResults] = useState<MultiTestResult[]>([]);
  const [detailSettingsFields, setDetailSettingsFields] = useState<ReturnType<typeof extractEditableFields>>([]);
  const [logLines, setLogLines] = useState<string[]>([]);
  const [logPath, setLogPath] = useState<string | null>(null);
  const [comboEstimate, setComboEstimate] = useState<ComboEstimate | null>(null);
  const [estimateLoading, setEstimateLoading] = useState<boolean>(false);
  const [estimateError, setEstimateError] = useState<string | null>(null);
  const [configJson, setConfigJson] = useState<string>("");
  const [configError, setConfigError] = useState<string | null>(null);
  const [paramGrid, setParamGrid] = useState<MultiTestParamGrid | null>(null);
  const [paramGridLoading, setParamGridLoading] = useState<boolean>(false);
  const [paramGridError, setParamGridError] = useState<string | null>(null);
  const [paramGridMessage, setParamGridMessage] = useState<string | null>(null);
  const [selectedStrategyGrid, setSelectedStrategyGrid] = useState<string>("swing_momentum");

  const loadTemplatesAndSchema = useCallback(async () => {
    try {
      const [templateResponse, settingsResponse, schemaResponse] = await Promise.all([
        fetchTemplates(),
        fetchCurrentSettings(),
        fetchSettingsSchema()
      ]);
      setTemplates(templateResponse.items);
      setTooltips(schemaResponse.tooltips ?? {});
      setSettingsPreview(settingsResponse.settings);
      setSettingsFields(extractEditableFields(settingsResponse.settings, schemaResponse.tooltips ?? {}));
    } catch (err) {
      console.error("Failed to load templates/settings:", err);
    }
  }, []);

  useEffect(() => {
    void loadTemplatesAndSchema();
  }, [loadTemplatesAndSchema]);

  const loadPresets = useCallback(async () => {
    try {
      const { items } = await fetchMultiTestPresets();
      setPresets(items);
      setPresetError(null);
      if (selectedPresetId && !items.some((preset) => preset.id === selectedPresetId)) {
        setSelectedPresetId(null);
      }
    } catch (err) {
      console.error("Failed to load presets:", err);
      setPresetError("Presets konnten nicht geladen werden.");
    }
  }, [selectedPresetId]);

  useEffect(() => {
    void loadPresets();
  }, [loadPresets]);

  const loadParamGrid = useCallback(async () => {
    setParamGridLoading(true);
    try {
      const data = await fetchMultiTestParamGrid();
      setParamGrid(data);
      setParamGridError(null);
      setSelectedStrategyGrid((prev) => {
        if (prev && data.strategies[prev]) {
          return prev;
        }
        const names = Object.keys(data.strategies || {});
        return names.length > 0 ? names[0] : "";
      });
    } catch (err) {
      console.error("Failed to load parameter grid:", err);
      setParamGridError("Parameterraster konnten nicht geladen werden.");
      setParamGrid(null);
    } finally {
      setParamGridLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadParamGrid();
  }, [loadParamGrid]);

  const refreshList = useCallback(async () => {
    setListLoading(true);
    try {
      const { items, active } = await listMultiTests(100, 0);
      setMultiTests(items);
      setActiveTest(active);
      setSelectedTestId((prevId) => {
        if (!prevId && (active?.id || items.length > 0)) {
          return active?.id ?? items[0].id;
        } else if (prevId) {
          const stillExists = items.some((item) => item.id === prevId);
          if (!stillExists) {
            return active?.id ?? items[0]?.id ?? null;
          }
        }
        return prevId;
      });
    } catch (err) {
      console.error("Failed to load multitests:", err);
    } finally {
      setListLoading(false);
    }
  }, []);

  useEffect(() => {
    void refreshList();
    const interval = window.setInterval(() => {
      void refreshList();
    }, 15000);
    return () => window.clearInterval(interval);
  }, [refreshList]);

  const loadSettingsPreview = useCallback(
    async (templateId: string) => {
      try {
        if (templateId === CURRENT_TEMPLATE_SENTINEL) {
          const current = await fetchCurrentSettings();
          setSettingsPreview(current.settings);
          setSettingsFields(extractEditableFields(current.settings, tooltips));
        } else {
          const tpl = await fetchTemplate(templateId);
          setSettingsPreview(tpl.payload);
          setSettingsFields(extractEditableFields(tpl.payload, tooltips));
        }
      } catch (err) {
        console.error("Failed to load template settings:", err);
      }
    },
    [tooltips]
  );

  useEffect(() => {
    void loadSettingsPreview(selectedTemplate);
  }, [selectedTemplate, loadSettingsPreview]);

  const handleStart = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setStartError(null);
    setStartSuccess(null);
    setStartLoading(true);
    try {
      const payload = buildStartPayload();
      const { multitest } = await startMultiTest(payload);
      setStartSuccess(`Multi-Test ${multitest.id} wurde gestartet.`);
      setSelectedTestId(multitest.id);
      await refreshList();
      await loadDetail(multitest.id);
    } catch (err: any) {
      console.error("Failed to start multi test:", err);
      const message = err?.response?.data?.detail ?? err?.message ?? "Multi-Test konnte nicht gestartet werden.";
      setStartError(message);
    } finally {
      setStartLoading(false);
    }
  };

  const handleApplyPreset = (preset: MultiTestPreset) => {
    const payload = preset.payload as Record<string, unknown>;
    const templateValue = payload["template_id"];
    if (typeof templateValue === "string") {
      setSelectedTemplate(templateValue);
    } else {
      setSelectedTemplate(CURRENT_TEMPLATE_SENTINEL);
    }
    const strategyValue = payload["strategy_name"];
    setStrategyName(typeof strategyValue === "string" ? strategyValue : "");

    const sampleValue = payload["sample_ratio"];
    if (typeof sampleValue === "number" && Number.isFinite(sampleValue)) {
      setSampleRatio(sampleValue);
    }

    const maxValue = payload["max_combos"];
    if (typeof maxValue === "number" && Number.isFinite(maxValue) && maxValue > 0) {
      setMaxCombos(Math.floor(maxValue));
    } else {
      setMaxCombos("");
    }

    const comboValue = payload["combo_timeout"];
    if (typeof comboValue === "number" && Number.isFinite(comboValue)) {
      setComboTimeout(comboValue);
    }

    const instrumentValue = payload["instrument_timeout"];
    if (typeof instrumentValue === "number" && Number.isFinite(instrumentValue)) {
      setInstrumentTimeout(instrumentValue);
    }

    const startValue = payload["start_date"];
    if (typeof startValue === "string") {
      setStartDate(startValue);
    }
    const endValue = payload["end_date"];
    if (typeof endValue === "string") {
      setEndDate(endValue);
    }

    const noteValue = payload["notes"];
    setNotes(typeof noteValue === "string" ? noteValue : "");

    setSelectedPresetId(preset.id);
    setPresetMessage(`Preset "${preset.name}" geladen.`);
    setPresetError(null);
  };

  const handleSavePreset = async () => {
    const name = window.prompt("Preset-Namen eingeben", strategyName || "Multi-Test Preset");
    const trimmedName = name?.trim();
    if (!trimmedName) {
      return;
    }
    const descriptionInput = window.prompt("Beschreibung (optional)", notes || "");
    const trimmedDescription = descriptionInput?.trim();
    const payload = buildPresetPayloadValues();
    try {
      const { preset } = await upsertMultiTestPreset({
        name: trimmedName,
        description: trimmedDescription || undefined,
        payload,
      });
      setPresetMessage(`Preset "${preset.name}" gespeichert.`);
      setPresetError(null);
      setSelectedPresetId(preset.id);
      await loadPresets();
    } catch (err: any) {
      console.error("Failed to save preset:", err);
      setPresetError(err?.response?.data?.detail ?? "Preset konnte nicht gespeichert werden.");
    }
  };

  const handleOverwritePreset = async () => {
    if (!selectedPresetId) {
      return;
    }
    const target = presets.find((item) => item.id === selectedPresetId);
    if (!target) {
      return;
    }
    const descriptionInput = window.prompt("Beschreibung (optional)", target.description ?? "");
    const trimmedDescription = descriptionInput?.trim();
    const payload = buildPresetPayloadValues();
    try {
      const { preset } = await upsertMultiTestPreset({
        id: target.id,
        name: target.name,
        description: trimmedDescription || undefined,
        payload,
      });
      setPresetMessage(`Preset "${preset.name}" aktualisiert.`);
      setPresetError(null);
      await loadPresets();
    } catch (err: any) {
      console.error("Failed to update preset:", err);
      setPresetError(err?.response?.data?.detail ?? "Preset konnte nicht aktualisiert werden.");
    }
  };

  const handleDeletePreset = async () => {
    if (!selectedPresetId) {
      return;
    }
    const target = presets.find((item) => item.id === selectedPresetId);
    if (!target) {
      return;
    }
    const confirmed = window.confirm(`Preset "${target.name}" wirklich löschen?`);
    if (!confirmed) {
      return;
    }
    try {
      await deleteMultiTestPreset(target.id);
      setPresetMessage(`Preset "${target.name}" gelöscht.`);
      setPresetError(null);
      setSelectedPresetId(null);
      await loadPresets();
    } catch (err: any) {
      console.error("Failed to delete preset:", err);
      setPresetError(err?.response?.data?.detail ?? "Preset konnte nicht gelöscht werden.");
    }
  };

  const stringifyValueList = (values?: unknown[]): string => {
    if (!values) {
      return "";
    }
    // Sicherstellen, dass values ein Array ist
    if (!Array.isArray(values)) {
      return "";
    }
    if (values.length === 0) {
      return "";
    }
    return values
      .map((value) => {
        if (value === null) return "null";
        if (typeof value === "boolean") return value ? "true" : "false";
        if (typeof value === "number") return Number.isInteger(value) ? value.toString() : value.toFixed(4).replace(/0+$/, "").replace(/\.$/, "");
        return String(value);
      })
      .join(", ");
  };

  const parseValueListInput = (raw: string): unknown[] => {
    if (!raw.trim()) {
      return [];
    }
    return raw
      .split(",")
      .map((token) => token.trim())
      .filter((token) => token.length > 0)
      .map((token) => {
        const lowered = token.toLowerCase();
        if (lowered === "true") return true;
        if (lowered === "false") return false;
        if (lowered === "null") return null;
        const numeric = Number(token);
        if (!Number.isNaN(numeric)) {
          return numeric;
        }
        return token;
      });
  };

  const updateParamGridLocal = (updater: (previous: MultiTestParamGrid) => MultiTestParamGrid) => {
    setParamGrid((prev) => {
      if (!prev) {
        return prev;
      }
      const next = updater(prev);
      setParamGridMessage(null);
      setParamGridError(null);
      return next;
    });
  };

  const handleGlobalParamChange = (param: string, raw: string) => {
    updateParamGridLocal((prev) => ({
      ...prev,
      global: {
        ...prev.global,
        [param]: parseValueListInput(raw),
      },
    }));
  };

  const handleStrategyParamChange = (strategy: string, param: string, raw: string) => {
    updateParamGridLocal((prev) => ({
      ...prev,
      strategies: {
        ...prev.strategies,
        [strategy]: {
          ...prev.strategies[strategy],
          [param]: parseValueListInput(raw),
        },
      },
    }));
  };

  const handleAddGlobalParam = () => {
    const key = window.prompt("Name des globalen Parameters");
    const trimmed = key?.trim();
    if (!trimmed) return;
    updateParamGridLocal((prev) => ({
      ...prev,
      global: {
        ...prev.global,
        [trimmed]: prev.global[trimmed] ?? [],
      },
    }));
  };

  const handleDeleteGlobalParam = (param: string) => {
    updateParamGridLocal((prev) => {
      const nextGlobal = { ...prev.global };
      delete nextGlobal[param];
      return { ...prev, global: nextGlobal };
    });
  };

  const handleAddStrategy = () => {
    const name = window.prompt("Strategiename", "custom_strategy");
    const trimmed = name?.trim();
    if (!trimmed) return;
    updateParamGridLocal((prev) => ({
      ...prev,
      strategies: {
        ...prev.strategies,
        [trimmed]: prev.strategies[trimmed] ?? {},
      },
    }));
    setSelectedStrategyGrid(trimmed);
  };

  const handleDeleteStrategy = (strategy: string) => {
    if (!paramGrid) return;
    const remaining = Object.keys(paramGrid.strategies ?? {}).filter((name) => name !== strategy);
    updateParamGridLocal((prev) => {
      const nextStrategies = { ...prev.strategies };
      delete nextStrategies[strategy];
      return { ...prev, strategies: nextStrategies };
    });
    setSelectedStrategyGrid((prev) => (prev === strategy ? (remaining.length > 0 ? remaining[0] : "") : prev));
  };

  const handleAddStrategyParam = (strategy: string) => {
    const key = window.prompt("Parametername", "neuer_parameter");
    const trimmed = key?.trim();
    if (!trimmed) return;
    updateParamGridLocal((prev) => ({
      ...prev,
      strategies: {
        ...prev.strategies,
        [strategy]: {
          ...prev.strategies[strategy],
          [trimmed]: prev.strategies[strategy]?.[trimmed] ?? [],
        },
      },
    }));
  };

  const handleDeleteStrategyParam = (strategy: string, param: string) => {
    updateParamGridLocal((prev) => {
      const nextStrategy = { ...(prev.strategies[strategy] ?? {}) };
      delete nextStrategy[param];
      return {
        ...prev,
        strategies: {
          ...prev.strategies,
          [strategy]: nextStrategy,
        },
      };
    });
  };

  const handleSaveParamGrid = async () => {
    if (!paramGrid) return;
    try {
      const saved = await updateMultiTestParamGrid(paramGrid);
      setParamGrid(saved);
      setSelectedStrategyGrid((prev) => {
        if (saved.strategies[prev]) return prev;
        const names = Object.keys(saved.strategies || {});
        return names.length > 0 ? names[0] : "";
      });
      setParamGridMessage("Parameterraster gespeichert.");
      setParamGridError(null);
    } catch (err: any) {
      console.error("Failed to save parameter grid:", err);
      setParamGridError(err?.response?.data?.detail ?? "Parameterraster konnte nicht gespeichert werden.");
    }
  };

  const handleResetParamGrid = async () => {
    try {
      const reset = await resetMultiTestParamGrid();
      setParamGrid(reset);
      setParamGridMessage("Parameterraster aus settings.json geladen.");
      setParamGridError(null);
      setSelectedStrategyGrid((prev) => {
        if (reset.strategies[prev]) return prev;
        const names = Object.keys(reset.strategies || {});
        return names.length > 0 ? names[0] : "";
      });
    } catch (err: any) {
      console.error("Failed to reset parameter grid:", err);
      setParamGridError(err?.response?.data?.detail ?? "Parameterraster konnte nicht zurückgesetzt werden.");
    }
  };

  const loadDetail = useCallback(
    async (multitestId: string) => {
      setDetailLoading(true);
      setDetailError(null);
      try {
        const { meta, results } = await fetchMultiTest(multitestId);
        setDetailMeta(meta);
        setDetailResults(results);
        if (meta.settings_snapshot) {
          setDetailSettingsFields(extractEditableFields(meta.settings_snapshot, tooltips));
        } else {
          setDetailSettingsFields([]);
        }
        try {
          const { config } = await fetchMultiTestConfig(multitestId);
          setConfigJson(JSON.stringify(config, null, 2));
          setConfigError(null);
        } catch (cfgErr: any) {
          console.error("Failed to load multitest config:", cfgErr);
          setConfigJson("");
          setConfigError(cfgErr?.response?.data?.detail ?? "Konfiguration konnte nicht geladen werden.");
        }
      } catch (err: any) {
        console.error("Failed to load multitest detail:", err);
        setDetailError(err?.response?.data?.detail ?? "Details konnten nicht geladen werden.");
        setDetailMeta(null);
        setDetailResults([]);
        setDetailSettingsFields([]);
        setConfigJson("");
        setConfigError(null);
      } finally {
        setDetailLoading(false);
      }
    },
    [tooltips]
  );

  const loadLog = useCallback(async (multitestId: string) => {
    try {
      const { lines, path } = await fetchMultiTestLog(multitestId, 200);
      setLogLines(lines);
      setLogPath(path);
    } catch (err) {
      console.error("Failed to fetch multitest log:", err);
      setLogLines([]);
      setLogPath(null);
    }
  }, []);

  const buildStartPayload = useCallback(() => {
    let maxCombosValue: number | undefined;
    if (maxCombos !== "") {
      const parsed = Number(maxCombos);
      if (!Number.isNaN(parsed) && parsed > 0) {
        maxCombosValue = Math.floor(parsed);
      }
    }

    return {
      template_id: selectedTemplate === CURRENT_TEMPLATE_SENTINEL ? undefined : selectedTemplate,
      strategy_name: strategyName.trim() || undefined,
      start_date: startDate,
      end_date: endDate,
      sample_ratio: sampleRatio,
      max_combos: maxCombosValue,
      combo_timeout: comboTimeout || undefined,
      instrument_timeout: instrumentTimeout || undefined,
      notes: notes.trim() || undefined,
    };
  }, [selectedTemplate, strategyName, startDate, endDate, sampleRatio, maxCombos, comboTimeout, instrumentTimeout, notes]);

  const buildPresetPayloadValues = useCallback((): Record<string, unknown> => {
    const payload = buildStartPayload();
    return {
      template_id: payload.template_id ?? null,
      strategy_name: payload.strategy_name ?? null,
      start_date: payload.start_date,
      end_date: payload.end_date,
      sample_ratio: payload.sample_ratio ?? null,
      max_combos: payload.max_combos ?? null,
      combo_timeout: payload.combo_timeout ?? null,
      instrument_timeout: payload.instrument_timeout ?? null,
      notes: payload.notes ?? null,
    };
  }, [buildStartPayload]);

  useEffect(() => {
    if (!selectedTestId) {
      setDetailMeta(null);
      setDetailResults([]);
      setDetailSettingsFields([]);
      setLogLines([]);
      setLogPath(null);
      setConfigJson("");
      setConfigError(null);
      return;
    }
    void loadDetail(selectedTestId);
    void loadLog(selectedTestId);
    const interval = window.setInterval(() => {
      void loadDetail(selectedTestId);
      void loadLog(selectedTestId);
    }, 10000);
    return () => window.clearInterval(interval);
  }, [selectedTestId, loadDetail, loadLog]);

  useEffect(() => {
    let ignore = false;
    const run = async () => {
      try {
        setEstimateLoading(true);
        const payload = buildStartPayload();
        const { estimate } = await estimateMultiTest(payload);
        if (!ignore) {
          setComboEstimate(estimate);
          setEstimateError(null);
        }
      } catch (err: any) {
        if (!ignore) {
          console.error("Failed to estimate combos:", err);
          setComboEstimate(null);
          setEstimateError(err?.response?.data?.detail ?? "Kombinationen konnten nicht berechnet werden.");
        }
      } finally {
        if (!ignore) {
          setEstimateLoading(false);
        }
      }
    };
    void run();
    return () => {
      ignore = true;
    };
  }, [buildStartPayload]);

  const handleSelectTest = async (test: MultiTestMeta) => {
    setSelectedTestId(test.id);
    await loadDetail(test.id);
    await loadLog(test.id);
  };

  const handleCancel = async (multitestId: string) => {
    try {
      await cancelMultiTest(multitestId);
      await refreshList();
      if (selectedTestId === multitestId) {
        await loadDetail(multitestId);
      }
    } catch (err: any) {
      console.error("Failed to cancel multitest:", err);
      alert(err?.response?.data?.detail ?? "Multi-Test konnte nicht abgebrochen werden.");
    }
  };

  const resultsByStrategy = useMemo(() => groupResultsByStrategy(detailResults), [detailResults]);

  const completedRuns = useMemo(
    () => multiTests.filter((test) => test.status === "completed").length,
    [multiTests]
  );
  const runningRuns = useMemo(
    () => multiTests.filter((test) => test.status === "running").length,
    [multiTests]
  );
  const queuedRuns = useMemo(
    () => multiTests.filter((test) => test.status === "queued").length,
    [multiTests]
  );

  return (
    <section className="page">
      <header className="page__header">
        <div>
          <h2>Multi-Test Manager</h2>
          <p>Starte, überwache und analysiere umfangreiche Optimierungs-Läufe.</p>
        </div>
      </header>

      <div className="card">
        <h3>Run-Übersicht</h3>
        <ul className="kv-list">
          <li>
            <span>Abgeschlossen</span>
            <strong>{completedRuns}</strong>
          </li>
          <li>
            <span>Laufend</span>
            <strong>{runningRuns}</strong>
          </li>
          <li>
            <span>Wartend</span>
            <strong>{queuedRuns}</strong>
          </li>
          <li>
            <span>Gesamt</span>
            <strong>{multiTests.length}</strong>
          </li>
        </ul>
      </div>

      <form className="card card--form" onSubmit={handleStart}>
        <h3>Neuen Multi-Test starten</h3>
        <div className="form-grid">
          <label>
            Vorlage
            <select value={selectedTemplate} onChange={(event) => setSelectedTemplate(event.target.value)}>
              <option value={CURRENT_TEMPLATE_SENTINEL}>Aktuelle Settings (settings.json)</option>
              {templates.map((template) => (
                <option key={template.id} value={template.id}>
                  {template.name}
                </option>
              ))}
            </select>
          </label>
          <label>
            Strategie (optional)
            <input
              type="text"
              value={strategyName}
              onChange={(event) => setStrategyName(event.target.value)}
              placeholder="z. B. swing_momentum"
            />
          </label>
          <label>
            Start-Datum
            <input type="date" value={startDate} onChange={(event) => setStartDate(event.target.value)} required />
          </label>
          <label>
            End-Datum
            <input type="date" value={endDate} onChange={(event) => setEndDate(event.target.value)} required />
          </label>
          <label>
            Sample Ratio
            <input
              type="number"
              step="0.01"
              min="0.01"
              max="1"
              value={sampleRatio}
              onChange={(event) => setSampleRatio(Number(event.target.value))}
            />
          </label>
          <label>
            Max Combos
            <input
              type="number"
              min="1"
              value={maxCombos === "" ? "" : Number(maxCombos)}
              onChange={(event) => {
                const raw = event.target.value;
                setMaxCombos(raw === "" ? "" : Number(raw));
              }}
              placeholder="leer = alle"
            />
          </label>
          <label>
            Combo Timeout (s)
            <input
              type="number"
              min="10"
              value={comboTimeout}
              onChange={(event) => setComboTimeout(Number(event.target.value))}
            />
          </label>
          <label>
            Instrument Timeout (s)
            <input
              type="number"
              min="10"
              value={instrumentTimeout}
              onChange={(event) => setInstrumentTimeout(Number(event.target.value))}
            />
          </label>
          <label className="form-grid__full">
            Notizen
            <input
              type="text"
              value={notes}
              onChange={(event) => setNotes(event.target.value)}
              placeholder="optional"
            />
          </label>
        </div>
        <div className="form-actions">
          <button type="submit" disabled={startLoading}>
            {startLoading ? "Starte..." : "Multi-Test starten"}
          </button>
          {activeTest && activeTest.status === "running" && (
            <button type="button" className="button-secondary" onClick={() => void handleCancel(activeTest.id)}>
              Laufenden Test abbrechen
            </button>
          )}
      </div>
      {startError && <div className="form-error">{startError}</div>}
      {startSuccess && <div className="form-success">{startSuccess}</div>}
    </form>

      <div className="card">
        <h3>Kombinationen (Vorschau)</h3>
        {estimateLoading && <p>Berechne Kombinationen...</p>}
        {estimateError && <p className="card__error">{estimateError}</p>}
        {!estimateLoading && !estimateError && comboEstimate && (
          <>
            <p>
              Gesamt effektive Kombos:
              <strong> {comboEstimate.total ?? "?"}</strong>
            </p>
            {comboEstimate.strategies.length > 0 ? (
              <div className="table-wrapper">
                <table className="table table--compact">
                  <thead>
                    <tr>
                      <th>Strategie</th>
                      <th>Alias</th>
                      <th>Basis</th>
                      <th>Effektiv</th>
                      <th>Begrenzt</th>
                    </tr>
                  </thead>
                  <tbody>
                    {comboEstimate.strategies.map((item) => (
                      <tr key={`${item.name}-${item.alias ?? ""}`}>
                        <td>{item.name}</td>
                        <td>{item.alias ?? "—"}</td>
                        <td>{item.base}</td>
                        <td>{item.effective}</td>
                        <td>{item.truncated ? "ja" : "nein"}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : (
              <p>Keine Strategien mit Parameterraster vorhanden.</p>
            )}
          </>
        )}
        {!estimateLoading && !estimateError && !comboEstimate && <p>Noch keine Vorschau verfügbar.</p>}
      </div>

      <div className="card">
        <h3>Parameter-Raster</h3>
        {paramGridMessage && <p className="card__hint">{paramGridMessage}</p>}
        {paramGridError && <p className="card__error">{paramGridError}</p>}
        {paramGridLoading && <p>Lade Parameterraster...</p>}
        {!paramGridLoading && paramGrid && (
          <div className="param-grid">
            <section>
              <div className="param-grid__header">
                <h4>Globale Parameter</h4>
                <button type="button" className="button-secondary" onClick={handleAddGlobalParam}>
                  Globalen Parameter hinzufügen
                </button>
              </div>
              {Object.keys(paramGrid.global).length === 0 ? (
                <p>Keine globalen Parameter definiert.</p>
              ) : (
                <div className="table-wrapper">
                  <table className="table table--compact">
                    <thead>
                      <tr>
                        <th>Parameter</th>
                        <th>Werte</th>
                        <th></th>
                      </tr>
                    </thead>
                    <tbody>
                      {Object.entries(paramGrid.global).map(([param, values]) => (
                        <tr key={param}>
                          <td>{param}</td>
                          <td>
                            <input
                              type="text"
                              value={stringifyValueList(values)}
                              onChange={(event) => handleGlobalParamChange(param, event.target.value)}
                              placeholder="Komma-separierte Werte"
                            />
                          </td>
                          <td>
                            <button type="button" className="button-secondary" onClick={() => handleDeleteGlobalParam(param)}>
                              Entfernen
                            </button>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </section>

            <section>
              <div className="param-grid__header">
                <h4>Strategie-Parameter</h4>
                <div className="param-grid__actions">
                  <button type="button" className="button-secondary" onClick={handleAddStrategy}>
                    Strategie hinzufügen
                  </button>
                  {selectedStrategyGrid && paramGrid.strategies[selectedStrategyGrid] && (
                    <button type="button" className="button-secondary" onClick={() => handleDeleteStrategy(selectedStrategyGrid)}>
                      Strategie entfernen
                    </button>
                  )}
                </div>
              </div>
              {Object.keys(paramGrid.strategies || {}).length === 0 ? (
                <p>Noch keine Strategien definiert.</p>
              ) : (
                <div className="param-grid__strategy">
                  <label>
                    Strategie auswählen
                    <select value={selectedStrategyGrid} onChange={(event) => setSelectedStrategyGrid(event.target.value)}>
                      {Object.keys(paramGrid.strategies).map((strategy) => (
                        <option key={strategy} value={strategy}>
                          {strategy}
                        </option>
                      ))}
                    </select>
                  </label>
                  {selectedStrategyGrid && paramGrid.strategies[selectedStrategyGrid] ? (
                    <>
                      <div className="param-grid__strategy-actions">
                        <button type="button" className="button-secondary" onClick={() => handleAddStrategyParam(selectedStrategyGrid)}>
                          Parameter hinzufügen
                        </button>
                      </div>
                      <div className="table-wrapper">
                        <table className="table table--compact">
                          <thead>
                            <tr>
                              <th>Parameter</th>
                              <th>Werte</th>
                              <th></th>
                            </tr>
                          </thead>
                          <tbody>
                            {Object.entries(paramGrid.strategies[selectedStrategyGrid]).map(([param, values]) => (
                              <tr key={param}>
                                <td>{param}</td>
                                <td>
                                  <input
                                    type="text"
                                    value={stringifyValueList(values)}
                                    onChange={(event) => handleStrategyParamChange(selectedStrategyGrid, param, event.target.value)}
                                    placeholder="Komma-separierte Werte"
                                  />
                                </td>
                                <td>
                                  <button type="button" className="button-secondary" onClick={() => handleDeleteStrategyParam(selectedStrategyGrid, param)}>
                                    Entfernen
                                  </button>
                                </td>
                              </tr>
                            ))}
                          </tbody>
                        </table>
                      </div>
                    </>
                  ) : (
                    <p>Bitte eine Strategie auswählen.</p>
                  )}
                </div>
              )}

              <div className="form-actions">
                <button type="button" onClick={handleSaveParamGrid} disabled={!paramGrid}>
                  Änderungen speichern
                </button>
                <button type="button" className="button-secondary" onClick={handleResetParamGrid}>
                  Aus settings.json laden
                </button>
              </div>
            </section>
          </div>
        )}
      </div>

      <div className="card">
        <h3>Presets</h3>
        {presetMessage && <p className="card__hint">{presetMessage}</p>}
        {presetError && <p className="card__error">{presetError}</p>}
        {presets.length === 0 ? (
          <p>Noch keine Presets gespeichert.</p>
        ) : (
          <div className="preset-list">
            {presets.map((preset) => (
              <button
                type="button"
                key={preset.id}
                className={`preset-pill ${selectedPresetId === preset.id ? "preset-pill--active" : ""}`}
                onClick={() => handleApplyPreset(preset)}
                title={`Aktualisiert: ${new Date(preset.updated_at).toLocaleString()}`}
              >
                <span>{preset.name}</span>
                {preset.description && <small>{preset.description}</small>}
              </button>
            ))}
          </div>
        )}
        <div className="form-actions">
          <button type="button" onClick={handleSavePreset}>
            Als Preset speichern
          </button>
          {selectedPresetId && (
            <>
              <button type="button" className="button-secondary" onClick={handleOverwritePreset}>
                Preset überschreiben
              </button>
              <button type="button" className="button-secondary" onClick={handleDeletePreset}>
                Preset löschen
              </button>
            </>
          )}
        </div>
      </div>

      <div className="card">
        <h3>Settings-Vorschau</h3>
        {settingsPreview ? (
          <div className="settings-preview">
            <table className="table table--compact">
              <thead>
                <tr>
                  <th>Feld</th>
                  <th>Wert</th>
                </tr>
              </thead>
              <tbody>
                {settingsFields.slice(0, 100).map((field) => {
                  const value = getNestedValue(settingsPreview, field.path);
                  return (
                    <tr key={field.path}>
                      <td>
                        <strong>{field.displayName}</strong>
                        <div className="table__path">{field.path}</div>
                      </td>
                      <td>
                        <code>{JSON.stringify(value ?? null)}</code>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
            {settingsFields.length > 100 && (
              <p className="card__hint">
                Es werden nur die ersten 100 Felder angezeigt. Details findest du in der Template-Ansicht.
              </p>
            )}
          </div>
        ) : (
          <p>Keine Settings verfügbar.</p>
        )}
      </div>

      <div className="card">
        <div className="card__header">
          <h3>Multi-Test Läufe</h3>
          {listLoading && <span className="badge badge--running">Aktualisiere...</span>}
        </div>
        {multiTests.length === 0 ? (
          <p>Noch keine Multi-Tests vorhanden.</p>
        ) : (
          <div className="table-wrapper">
            <table className="table">
              <thead>
                <tr>
                  <th>ID</th>
                  <th>Status</th>
                  <th>Strategie</th>
                  <th>Zeitraum</th>
                  <th>Template</th>
                  <th>Fortschritt</th>
                  <th>Gestartet</th>
                  <th>Fertig</th>
                  <th>Notizen</th>
                  <th>Aktion</th>
                </tr>
              </thead>
              <tbody>
                {multiTests.map((test) => (
                  <tr key={test.id} className={selectedTestId === test.id ? "table__row--active" : ""}>
                    <td>{test.id}</td>
                    <td>
                      <span className={`badge badge--${test.status}`}>{test.status}</span>
                    </td>
                    <td>{test.strategy_name ?? "Alle"}</td>
                    <td>
                      {test.start_date} → {test.end_date}
                    </td>
                    <td>{test.template_name ?? (test.template_id ? test.template_id : "Aktuelle Settings")}</td>
                    <td>
                      {test.completed_combos ?? 0}/{test.total_combos ?? "?"}
                      {typeof test.remaining_combos === "number" && test.total_combos != null && (
                        <div className="table__path">Verbleibend: {test.remaining_combos}</div>
                      )}
                    </td>
                    <td>{formatDate(test.started_at)}</td>
                    <td>{formatDate(test.completed_at)}</td>
                    <td>{test.notes ?? "—"}</td>
                    <td>
                      <div className="table__actions">
                        <button type="button" onClick={() => void handleSelectTest(test)}>
                          Anzeigen
                        </button>
                        {(test.status === "running" || test.status === "queued") && (
                          <button
                            type="button"
                            className="button-secondary"
                            onClick={() => void handleCancel(test.id)}
                          >
                            Abbrechen
                          </button>
                        )}
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      <div className="grid grid--cols-2 grid--gap-lg">
        <div className="card">
          <h3>Details</h3>
          {detailLoading && <p>Lade Details...</p>}
          {detailError && <p className="card__error">{detailError}</p>}
          {detailMeta && (
            <div className="kv-list">
              <div>
                <span>Status</span>
                <strong className={`badge badge--${detailMeta.status}`}>{detailMeta.status}</strong>
              </div>
              <div>
                <span>Strategie</span>
                <strong>{detailMeta.strategy_name ?? "Alle"}</strong>
              </div>
              <div>
                <span>Zeitraum</span>
                <strong>
                  {detailMeta.start_date} → {detailMeta.end_date}
                </strong>
              </div>
              <div>
                <span>Gestartet</span>
                <strong>{formatDate(detailMeta.started_at)}</strong>
              </div>
              <div>
                <span>Fertig</span>
                <strong>{formatDate(detailMeta.completed_at)}</strong>
              </div>
              <div>
                <span>Sample Ratio</span>
                <strong>{detailMeta.sample_ratio ?? "—"}</strong>
              </div>
              <div>
                <span>Max Combos</span>
                <strong>{detailMeta.max_combos ?? "—"}</strong>
              </div>
              <div>
                <span>Notizen</span>
                <strong>{detailMeta.notes ?? "—"}</strong>
              </div>
              <div>
                <span>Fehler</span>
                <strong className="text-danger">{detailMeta.error ?? "—"}</strong>
              </div>
              <div>
                <span>Log</span>
                <strong>{logPath ?? "—"}</strong>
              </div>
              <div>
                <span>Kombinationen (fertig/gesamt)</span>
                <strong>
                  {detailMeta.completed_combos ?? 0}/{detailMeta.total_combos ?? "?"}
                </strong>
              </div>
              <div>
                <span>Noch verbleibend</span>
                <strong>{detailMeta.remaining_combos ?? "?"}</strong>
              </div>
            </div>
          )}
        </div>

        <div className="card">
          <h3>Log</h3>
          {logLines.length === 0 ? (
            <p>Keine Logzeilen verfügbar.</p>
          ) : (
            <pre className="log-viewer">
              {logLines.map((line, idx) => (
                <span key={idx}>{line}</span>
              ))}
            </pre>
          )}
        </div>
      </div>

      <div className="card">
        <h3>Settings des ausgewählten Multi-Tests</h3>
        {detailMeta?.settings_snapshot ? (
          <div className="settings-preview">
            <table className="table table--compact">
              <thead>
                <tr>
                  <th>Feld</th>
                  <th>Wert</th>
                </tr>
              </thead>
              <tbody>
                {detailSettingsFields.slice(0, 150).map((field) => {
                  const value = detailMeta?.settings_snapshot
                    ? getNestedValue(detailMeta.settings_snapshot, field.path)
                    : null;
                  return (
                    <tr key={field.path}>
                      <td>
                        <strong>{field.displayName}</strong>
                        <div className="table__path">{field.path}</div>
                      </td>
                      <td>
                        <code>{JSON.stringify(value ?? null)}</code>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
            {detailSettingsFields.length > 150 && (
              <p className="card__hint">Die Ansicht ist auf 150 Felder begrenzt.</p>
            )}
          </div>
        ) : (
          <p>Keine Settings gespeichert.</p>
        )}
      </div>

      <div className="card">
        <h3>Konfiguration</h3>
        {configError && <p className="card__error">{configError}</p>}
        {!configError && configJson && <pre className="log-viewer">{configJson}</pre>}
        {!configError && !configJson && <p>Keine Konfiguration verfügbar.</p>}
      </div>

      <div className="card">
        <h3>Leaderboards</h3>
        {detailResults.length === 0 ? (
          <p>Noch keine Ergebnisse verfügbar.</p>
        ) : (
          Object.entries(resultsByStrategy).map(([strategy, rows]) => (
            <div key={strategy} className="table-wrapper">
              <h4>{strategy}</h4>
              <table className="table table--compact">
                <thead>
                  <tr>
                    <th>#</th>
                    <th>Combo</th>
                    <th>PnL</th>
                    <th>Trades</th>
                    <th>PnL/Trade</th>
                    <th>Winrate</th>
                    <th>PnL Long</th>
                    <th>PnL/Trade Long</th>
                    <th>PnL Short</th>
                    <th>PnL/Trade Short</th>
                    <th>Parameter</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((row) => {
                    const pnlPerTrade = row.pnl_per_trade ?? (row.trades > 0 ? row.pnl / row.trades : 0);
                    const pnlPerTradeLong = row.pnl_per_trade_long ?? (row.params?.trades_long > 0 ? row.pnl_long / row.params.trades_long : null);
                    const pnlPerTradeShort = row.pnl_per_trade_short ?? (row.params?.trades_short > 0 ? row.pnl_short / row.params.trades_short : null);
                    return (
                      <tr key={row.combo_id}>
                        <td>{row.leaderboard_rank}</td>
                        <td>{row.combo_id}</td>
                        <td>{formatNumber(row.pnl)}</td>
                        <td>{row.trades}</td>
                        <td>{formatNumber(pnlPerTrade, 4)}</td>
                        <td>{formatNumber(row.winrate * 100, 1)}%</td>
                        <td>{formatNumber(row.pnl_long)}</td>
                        <td>{pnlPerTradeLong != null ? formatNumber(pnlPerTradeLong, 4) : "—"}</td>
                        <td>{formatNumber(row.pnl_short)}</td>
                        <td>{pnlPerTradeShort != null ? formatNumber(pnlPerTradeShort, 4) : "—"}</td>
                        <td>
                          <code>{JSON.stringify(row.params)}</code>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          ))
        )}
      </div>
    </section>
  );
}
