import { useState, useEffect } from "react";
import {
  updateStrategyBranch,
  fetchStrategyBranch,
  type StrategyBranch,
  type StrategyMetadataResponse,
} from "../api/client";

interface StrategyPreset {
  id: string;
  name: string;
  description?: string;
  strategy: string;
  variation: string;
  direction: string;
  symbol: string;
  params: Record<string, unknown>;
  created_at: string;
}

interface StrategyParameterEditorProps {
  branch: StrategyBranch;
  metadata: StrategyMetadataResponse;
  onUpdate: () => void;
}

const PRESET_STORAGE_KEY = "strategy_control_presets";

function loadPresets(): StrategyPreset[] {
  try {
    const stored = localStorage.getItem(PRESET_STORAGE_KEY);
    return stored ? JSON.parse(stored) : [];
  } catch {
    return [];
  }
}

function savePresets(presets: StrategyPreset[]): void {
  try {
    localStorage.setItem(PRESET_STORAGE_KEY, JSON.stringify(presets));
  } catch (err) {
    console.error("Failed to save presets:", err);
  }
}

export function StrategyParameterEditor({
  branch,
  metadata,
  onUpdate,
}: StrategyParameterEditorProps): JSX.Element {
  const [editedParams, setEditedParams] = useState<Record<string, unknown>>({});
  const [saving, setSaving] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [presets, setPresets] = useState<StrategyPreset[]>([]);
  const [showPresetDialog, setShowPresetDialog] = useState<boolean>(false);
  const [presetName, setPresetName] = useState<string>("");
  const [presetDescription, setPresetDescription] = useState<string>("");

  const strategy = metadata.strategies[branch.strategy];
  const paramInfos = strategy?.parameters || {};

  useEffect(() => {
    // Initialisiere editedParams mit aktuellen Werten
    setEditedParams(branch.params);
    // Lade Presets
    setPresets(loadPresets().filter(
      (p) =>
        p.strategy === branch.strategy &&
        p.variation === branch.variation &&
        p.direction === branch.direction &&
        p.symbol === branch.symbol
    ));
  }, [branch]);

  const handleParamChange = (paramName: string, value: unknown) => {
    setEditedParams((prev) => ({
      ...prev,
      [paramName]: value,
    }));
    setError(null);
    setMessage(null);
  };

  const handleSave = async () => {
    try {
      setSaving(true);
      setError(null);
      setMessage(null);
      await updateStrategyBranch(
        branch.strategy,
        branch.variation,
        branch.direction,
        branch.symbol,
        editedParams
      );
      setMessage("Settings wurden gespeichert. Beim nächsten Deploy werden sie angewendet.");
      onUpdate();
    } catch (err) {
      console.error(err);
      setError("Fehler beim Speichern der Settings.");
    } finally {
      setSaving(false);
    }
  };

  const handleReset = async () => {
    try {
      const branchDetail = await fetchStrategyBranch(
        branch.strategy,
        branch.variation,
        branch.direction,
        branch.symbol
      );
      setEditedParams(branchDetail.params);
      setError(null);
      setMessage(null);
    } catch (err) {
      console.error(err);
      setError("Fehler beim Laden der Settings.");
    }
  };

  const handleSavePreset = () => {
    if (!presetName.trim()) {
      setError("Preset-Name ist erforderlich.");
      return;
    }
    const newPreset: StrategyPreset = {
      id: `${Date.now()}-${Math.random().toString(36).substr(2, 9)}`,
      name: presetName.trim(),
      description: presetDescription.trim() || undefined,
      strategy: branch.strategy,
      variation: branch.variation,
      direction: branch.direction,
      symbol: branch.symbol,
      params: { ...editedParams },
      created_at: new Date().toISOString(),
    };
    const allPresets = loadPresets();
    allPresets.push(newPreset);
    savePresets(allPresets);
    setPresets(allPresets.filter(
      (p) =>
        p.strategy === branch.strategy &&
        p.variation === branch.variation &&
        p.direction === branch.direction &&
        p.symbol === branch.symbol
    ));
    setShowPresetDialog(false);
    setPresetName("");
    setPresetDescription("");
    setMessage(`Preset "${newPreset.name}" gespeichert.`);
  };

  const handleLoadPreset = (preset: StrategyPreset) => {
    setEditedParams(preset.params);
    setMessage(`Preset "${preset.name}" geladen.`);
  };

  const handleDeletePreset = (presetId: string) => {
    if (!window.confirm("Preset wirklich löschen?")) {
      return;
    }
    const allPresets = loadPresets();
    const filtered = allPresets.filter((p) => p.id !== presetId);
    savePresets(filtered);
    setPresets(filtered.filter(
      (p) =>
        p.strategy === branch.strategy &&
        p.variation === branch.variation &&
        p.direction === branch.direction &&
        p.symbol === branch.symbol
    ));
    setMessage("Preset gelöscht.");
  };

  const getParamValue = (paramName: string): unknown => {
    return editedParams[paramName] ?? branch.params[paramName];
  };

  const getCurrentValue = (paramName: string): unknown => {
    return branch.current_params?.[paramName] ?? branch.params[paramName];
  };

  const renderParamInput = (paramName: string, value: unknown, currentValue: unknown) => {
    const paramInfo = paramInfos[paramName];
    const description = paramInfo?.description || "Keine Beschreibung verfügbar.";
    const defaultValue = paramInfo?.default;

    const valueType = typeof value;
    const hasChanges = JSON.stringify(value) !== JSON.stringify(currentValue);

    if (valueType === "boolean") {
      return (
        <tr key={paramName} style={{ backgroundColor: hasChanges ? "#fff9e6" : "transparent" }}>
          <td style={{ padding: "0.5rem", verticalAlign: "top" }}>
            <div>
              <strong>{paramName}</strong>
              <br />
              <code style={{ fontSize: "0.8rem", color: "#666" }}>{paramName}</code>
              <br />
              <small style={{ color: "#666", fontStyle: "italic" }}>{description}</small>
            </div>
          </td>
          <td style={{ padding: "0.5rem", verticalAlign: "top" }}>
            <div style={{ display: "flex", flexDirection: "column", gap: "0.25rem" }}>
              <div>
                <small style={{ color: "#999" }}>Aktuell (Run):</small>
                <div style={{ padding: "0.25rem", backgroundColor: "#f5f5f5", borderRadius: "4px" }}>
                  {String(currentValue ?? "—")}
                </div>
              </div>
              <div>
                <small style={{ color: "#999" }}>Neu (nächster Deploy):</small>
                <label style={{ display: "flex", alignItems: "center", gap: "0.5rem" }}>
                  <input
                    type="checkbox"
                    checked={Boolean(value)}
                    onChange={(e) => handleParamChange(paramName, e.target.checked)}
                  />
                  <span>{String(value)}</span>
                </label>
              </div>
            </div>
          </td>
        </tr>
      );
    }

    if (valueType === "number") {
      return (
        <tr key={paramName} style={{ backgroundColor: hasChanges ? "#fff9e6" : "transparent" }}>
          <td style={{ padding: "0.5rem", verticalAlign: "top" }}>
            <div>
              <strong>{paramName}</strong>
              <br />
              <code style={{ fontSize: "0.8rem", color: "#666" }}>{paramName}</code>
              <br />
              <small style={{ color: "#666", fontStyle: "italic" }}>{description}</small>
              {defaultValue !== undefined && (
                <>
                  <br />
                  <small style={{ color: "#999" }}>Default: {String(defaultValue)}</small>
                </>
              )}
            </div>
          </td>
          <td style={{ padding: "0.5rem", verticalAlign: "top" }}>
            <div style={{ display: "flex", flexDirection: "column", gap: "0.25rem" }}>
              <div>
                <small style={{ color: "#999" }}>Aktuell (Run):</small>
                <div style={{ padding: "0.25rem", backgroundColor: "#f5f5f5", borderRadius: "4px" }}>
                  {currentValue !== null && currentValue !== undefined
                    ? String(currentValue)
                    : "—"}
                </div>
              </div>
              <div>
                <small style={{ color: "#999" }}>Neu (nächster Deploy):</small>
                <input
                  type="number"
                  step="any"
                  value={value !== null && value !== undefined ? String(value) : ""}
                  onChange={(e) => {
                    const numValue = e.target.value === "" ? null : Number(e.target.value);
                    handleParamChange(paramName, numValue);
                  }}
                  style={{ width: "100%", padding: "0.25rem" }}
                />
              </div>
            </div>
          </td>
        </tr>
      );
    }

    // String or other
    return (
      <tr key={paramName} style={{ backgroundColor: hasChanges ? "#fff9e6" : "transparent" }}>
        <td style={{ padding: "0.5rem", verticalAlign: "top" }}>
          <div>
            <strong>{paramName}</strong>
            <br />
            <code style={{ fontSize: "0.8rem", color: "#666" }}>{paramName}</code>
            <br />
            <small style={{ color: "#666", fontStyle: "italic" }}>{description}</small>
            {defaultValue !== undefined && (
              <>
                <br />
                <small style={{ color: "#999" }}>Default: {String(defaultValue)}</small>
              </>
            )}
          </div>
        </td>
        <td style={{ padding: "0.5rem", verticalAlign: "top" }}>
          <div style={{ display: "flex", flexDirection: "column", gap: "0.25rem" }}>
            <div>
              <small style={{ color: "#999" }}>Aktuell (Run):</small>
              <div style={{ padding: "0.25rem", backgroundColor: "#f5f5f5", borderRadius: "4px" }}>
                {currentValue !== null && currentValue !== undefined
                  ? String(currentValue)
                  : "—"}
              </div>
            </div>
            <div>
              <small style={{ color: "#999" }}>Neu (nächster Deploy):</small>
              <input
                type="text"
                value={value !== null && value !== undefined ? String(value) : ""}
                onChange={(e) => handleParamChange(paramName, e.target.value)}
                style={{ width: "100%", padding: "0.25rem" }}
              />
            </div>
          </div>
        </td>
      </tr>
    );
  };

  const paramNames = Object.keys(branch.params).sort();

  return (
    <div>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "1rem" }}>
        <h4>Parameter</h4>
        <div style={{ display: "flex", gap: "0.5rem", flexWrap: "wrap" }}>
          <button
            type="button"
            className="button button--ghost"
            onClick={() => setShowPresetDialog(true)}
            disabled={saving}
          >
            Preset speichern
          </button>
          <button
            type="button"
            className="button button--ghost"
            onClick={handleReset}
            disabled={saving}
          >
            Zurücksetzen
          </button>
          <button
            type="button"
            className="button"
            onClick={handleSave}
            disabled={saving}
          >
            {saving ? "Speichere..." : "Settings speichern"}
          </button>
        </div>
      </div>

      {presets.length > 0 && (
        <div style={{ marginBottom: "1rem", padding: "0.5rem", backgroundColor: "#f9f9f9", borderRadius: "4px" }}>
          <strong style={{ fontSize: "0.9rem" }}>Presets:</strong>
          <div style={{ display: "flex", flexWrap: "wrap", gap: "0.5rem", marginTop: "0.5rem" }}>
            {presets.map((preset) => (
              <div
                key={preset.id}
                style={{
                  display: "flex",
                  alignItems: "center",
                  gap: "0.25rem",
                  padding: "0.25rem 0.5rem",
                  backgroundColor: "white",
                  borderRadius: "4px",
                  border: "1px solid #ddd",
                }}
              >
                <button
                  type="button"
                  className="button button--ghost"
                  style={{ fontSize: "0.8rem", padding: "0.25rem 0.5rem" }}
                  onClick={() => handleLoadPreset(preset)}
                >
                  {preset.name}
                </button>
                <button
                  type="button"
                  className="button button--ghost"
                  style={{ fontSize: "0.8rem", padding: "0.25rem", color: "#d00" }}
                  onClick={() => handleDeletePreset(preset.id)}
                  title="Löschen"
                >
                  ×
                </button>
              </div>
            ))}
          </div>
        </div>
      )}

      {showPresetDialog && (
        <div
          style={{
            position: "fixed",
            top: 0,
            left: 0,
            right: 0,
            bottom: 0,
            backgroundColor: "rgba(0,0,0,0.5)",
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            zIndex: 1000,
          }}
          onClick={() => setShowPresetDialog(false)}
        >
          <div
            className="card"
            style={{ width: "400px", maxWidth: "90vw" }}
            onClick={(e) => e.stopPropagation()}
          >
            <h4>Preset speichern</h4>
            <div style={{ display: "flex", flexDirection: "column", gap: "0.5rem", marginBottom: "1rem" }}>
              <label>
                Name *
                <input
                  type="text"
                  value={presetName}
                  onChange={(e) => setPresetName(e.target.value)}
                  style={{ width: "100%", padding: "0.25rem" }}
                  placeholder={`${branch.strategy}_${branch.variation}_${branch.direction}_${branch.symbol}`}
                />
              </label>
              <label>
                Beschreibung
                <textarea
                  value={presetDescription}
                  onChange={(e) => setPresetDescription(e.target.value)}
                  style={{ width: "100%", padding: "0.25rem", minHeight: "60px" }}
                  placeholder="Optionale Beschreibung..."
                />
              </label>
            </div>
            <div style={{ display: "flex", gap: "0.5rem", justifyContent: "flex-end" }}>
              <button
                type="button"
                className="button button--ghost"
                onClick={() => {
                  setShowPresetDialog(false);
                  setPresetName("");
                  setPresetDescription("");
                }}
              >
                Abbrechen
              </button>
              <button type="button" className="button" onClick={handleSavePreset}>
                Speichern
              </button>
            </div>
          </div>
        </div>
      )}

      {error && <div className="card card--error" style={{ marginBottom: "1rem" }}>{error}</div>}
      {message && <div className="card card--success" style={{ marginBottom: "1rem" }}>{message}</div>}

      {paramNames.length === 0 ? (
        <p>Keine Parameter verfügbar.</p>
      ) : (
        <div className="settings-table-wrapper">
          <table className="table settings-table">
            <thead>
              <tr>
                <th>Parameter</th>
                <th>Wert</th>
              </tr>
            </thead>
            <tbody>
              {paramNames.map((paramName) => {
                const value = getParamValue(paramName);
                const currentValue = getCurrentValue(paramName);
                return renderParamInput(paramName, value, currentValue);
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

