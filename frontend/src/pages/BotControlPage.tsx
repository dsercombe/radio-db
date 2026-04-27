import { FormEvent, useEffect, useMemo, useState } from "react";
import {
  deploySettings,
  fetchStatus,
  fetchCurrentSettings,
  fetchTemplates,
  fetchTemplate,
  deleteTemplate,
  fetchSettingsSchema,
  fetchVisibleSettingsFields,
  type TemplateMeta
} from "../api/client";
import { extractEditableFields, getNestedValue, setNestedValueImmutable } from "../utils/settings";

type Primitive = string | number | boolean | null;

export function BotControlPage(): JSX.Element {
  const [templateName, setTemplateName] = useState<string>("");
  const [templateDescription, setTemplateDescription] = useState<string>("");
  const [templateTags, setTemplateTags] = useState<string>("");
  const [saveTemplate, setSaveTemplate] = useState<boolean>(false);

  const [settingsObject, setSettingsObject] = useState<Record<string, unknown> | null>(null);
  const [settingsJson, setSettingsJson] = useState<string>("{}");
  const [jsonError, setJsonError] = useState<string | null>(null);
  const [tooltips, setTooltips] = useState<Record<string, string>>({});
  const [hiddenFields, setHiddenFields] = useState<Set<string> | null>(null);

  const [templates, setTemplates] = useState<TemplateMeta[]>([]);
  const [loadingTemplates, setLoadingTemplates] = useState<boolean>(true);

  const [deploying, setDeploying] = useState<boolean>(false);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loadingSettings, setLoadingSettings] = useState<boolean>(true);

  const [fieldDrafts, setFieldDrafts] = useState<Record<string, string>>({});
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({});
  const [filterSearch, setFilterSearch] = useState<string>("");

  const loadInitialData = async () => {
    try {
      setLoadingSettings(true);
      setLoadingTemplates(true);
      const [currentSettings, schemaResponse, templateResponse, hiddenResponse] = await Promise.all([
        fetchCurrentSettings(),
        fetchSettingsSchema(),
        fetchTemplates(),
        fetchVisibleSettingsFields()
      ]);
      const current = currentSettings.settings as Record<string, unknown>;
      setSettingsObject(current);
      setSettingsJson(JSON.stringify(current, null, 2));
      setTooltips(schemaResponse.tooltips ?? {});
      setTemplates(templateResponse.items);
      setHiddenFields(new Set(hiddenResponse ?? []));
      setJsonError(null);
      setFieldDrafts({});
      setFieldErrors({});
      setTemplateName("");
      setTemplateDescription("");
      setTemplateTags("");
    } catch (err) {
      console.error(err);
      setError("Initiale Daten konnten nicht geladen werden.");
    } finally {
      setLoadingSettings(false);
      setLoadingTemplates(false);
    }
  };

  useEffect(() => {
    loadInitialData();
  }, []);

  const handleRefreshSettings = async () => {
    await loadInitialData();
    setMessage("Settings wurden neu geladen.");
  };

  const allFields = useMemo(
    () => extractEditableFields(settingsObject, tooltips),
    [settingsObject, tooltips]
  );

  const visibleFields = useMemo(() => {
    if (!hiddenFields) {
      return allFields;
    }
    return allFields.filter((field) => !hiddenFields.has(field.path));
  }, [allFields, hiddenFields]);

  const filteredFields = useMemo(() => {
    if (!filterSearch.trim()) {
      return visibleFields;
    }
    const needle = filterSearch.trim().toLowerCase();
    return visibleFields.filter(
      (field) =>
        field.displayName.toLowerCase().includes(needle) || field.path.toLowerCase().includes(needle)
    );
  }, [visibleFields, filterSearch]);

  const visibleFieldCount = filteredFields.length;

  const updateSettingAtPath = (path: string, value: Primitive | Record<string, unknown> | unknown[]) => {
    setSettingsObject((prev) => {
      const next = setNestedValueImmutable(prev, path, value);
      setSettingsJson(JSON.stringify(next, null, 2));
      setJsonError(null);
      return next;
    });
  };

  const clearFieldError = (path: string) => {
    setFieldErrors((prev) => {
      if (!(path in prev)) return prev;
      const next = { ...prev };
      delete next[path];
      return next;
    });
  };

  const handleStringChange = (path: string, raw: string) => {
    clearFieldError(path);
    updateSettingAtPath(path, raw);
  };

  const handleNumberChange = (path: string, raw: string) => {
    if (!raw.trim()) {
      clearFieldError(path);
      updateSettingAtPath(path, null);
      return;
    }
    const parsed = Number(raw);
    if (Number.isNaN(parsed)) {
      setFieldErrors((prev) => ({ ...prev, [path]: "Ungültige Zahl" }));
      return;
    }
    clearFieldError(path);
    updateSettingAtPath(path, parsed);
  };

  const handleBooleanChange = (path: string, checked: boolean) => {
    clearFieldError(path);
    updateSettingAtPath(path, checked);
  };

  const handleJsonFieldChange = (path: string, raw: string) => {
    if (!raw.trim()) {
      clearFieldError(path);
      setFieldDrafts((prev) => {
        if (!(path in prev)) return prev;
        const next = { ...prev };
        delete next[path];
        return next;
      });
      updateSettingAtPath(path, null);
      return;
    }
    try {
      const parsed = JSON.parse(raw);
      clearFieldError(path);
      updateSettingAtPath(path, parsed as Record<string, unknown> | unknown[]);
      setFieldDrafts((prev) => {
        if (!(path in prev)) return prev;
        const next = { ...prev };
        delete next[path];
        return next;
      });
    } catch (err) {
      setFieldErrors((prev) => ({ ...prev, [path]: "Ungültiges JSON" }));
      setFieldDrafts((prev) => ({ ...prev, [path]: raw }));
    }
  };

  const handleRawJsonChange = (value: string) => {
    setSettingsJson(value);
    try {
      if (!value.trim()) {
        throw new Error("leer");
      }
      const parsed = JSON.parse(value);
      setSettingsObject(parsed as Record<string, unknown>);
      setJsonError(null);
      setFieldDrafts({});
      setFieldErrors({});
    } catch (err) {
      setJsonError("Settings JSON ist ungültig. Änderungen werden nicht übernommen.");
    }
  };

  const handleDeploy = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setError(null);
    setMessage(null);

    if (!settingsObject) {
      setError("Settings wurden noch nicht geladen.");
      return;
    }
    if (jsonError) {
      setError("Bitte behebe die Fehler im Raw JSON.");
      return;
    }
    if (Object.keys(fieldErrors).length > 0) {
      setError("Bitte behebe die markierten Feldfehler.");
      return;
    }

    setDeploying(true);
    try {
      const response = await deploySettings({
        settings: settingsObject,
        template_name: templateName || undefined,
        save_template: saveTemplate,
        template_description: templateDescription || undefined,
        tags: templateTags ? templateTags.split(",").map((tag) => tag.trim()).filter(Boolean) : undefined
      });
      setMessage(`Run ${response.run_id} gestartet.`);
      if (saveTemplate) {
        try {
          const data = await fetchTemplates();
          setTemplates(data.items);
        } catch (err) {
          console.error(err);
        }
      }
    } catch (err) {
      console.error(err);
      setError("Deploy fehlgeschlagen. Details im Log prüfen.");
    } finally {
      setDeploying(false);
    }
  };

  const handleApplyTemplate = async (id: string) => {
    try {
      const data = await fetchTemplate(id);
      const payload = data.payload as Record<string, unknown>;
      setSettingsObject(payload);
      setSettingsJson(JSON.stringify(payload, null, 2));
      setJsonError(null);
      setFieldDrafts({});
      setFieldErrors({});
      if (data.meta) {
        setTemplateName(data.meta.name ?? "");
        setTemplateDescription(data.meta.description ?? "");
        setTemplateTags((data.meta.tags ?? []).join(", "));
      }
      setMessage(`Template ${data.meta?.name ?? id} geladen.`);
    } catch (err) {
      console.error(err);
      setError("Template konnte nicht geladen werden.");
    }
  };

  const handleDeleteTemplate = async (id: string) => {
    if (!window.confirm("Template wirklich löschen?")) {
      return;
    }
    try {
      await deleteTemplate(id);
      setTemplates((prev) => prev.filter((tpl) => tpl.id !== id));
      setMessage(`Template ${id} gelöscht.`);
    } catch (err) {
      console.error(err);
      setError("Template konnte nicht gelöscht werden.");
    }
  };

  const handleRefresh = async () => {
    try {
      const status = await fetchStatus();
      setMessage(`Status: ${status.status} (Run ${status.run_id ?? "—"})`);
    } catch (err) {
      console.error(err);
      setError("Konnte Status nicht laden.");
    }
  };

  const buildFieldInput = (path: string, disabled: boolean): JSX.Element => {
    const rawValue = getNestedValue(settingsObject, path);
    const draft = fieldDrafts[path];
    const errorMessage = fieldErrors[path];
    const valueType = typeof rawValue;

    if (path === "kraken.demo_execution_mode") {
      const valueString = draft ?? (rawValue ? String(rawValue) : "kraken");
      return (
        <>
          <select
            className="settings-field__input"
            value={valueString}
            onChange={(event) => handleStringChange(path, event.target.value)}
            disabled={disabled}
          >
            <option value="kraken">Kraken Demo</option>
            <option value="internal">Interne Simulation</option>
          </select>
          {errorMessage && <p className="settings-field__error">{errorMessage}</p>}
        </>
      );
    }

    if (path.endsWith("enabled") && valueType === "boolean") {
      return (
        <>
          <label className="settings-field__checkbox">
            <input
              type="checkbox"
              checked={Boolean(rawValue)}
              onChange={(event) => handleBooleanChange(path, event.target.checked)}
              disabled={disabled}
            />
            <span>Aktiviert</span>
          </label>
          {errorMessage && <p className="settings-field__error">{errorMessage}</p>}
        </>
      );
    }

    if (valueType === "number") {
      const valueString = rawValue === null || rawValue === undefined ? "" : String(rawValue);
      return (
        <>
          <input
            className="settings-field__input"
            type="number"
            value={valueString}
            onChange={(event) => handleNumberChange(path, event.target.value)}
            disabled={disabled}
          />
          {errorMessage && <p className="settings-field__error">{errorMessage}</p>}
        </>
      );
    }

    if (valueType === "boolean") {
      return (
        <>
          <label className="settings-field__checkbox">
            <input
              type="checkbox"
              checked={Boolean(rawValue)}
              onChange={(event) => handleBooleanChange(path, event.target.checked)}
              disabled={disabled}
            />
            <span>Aktiviert</span>
          </label>
          {errorMessage && <p className="settings-field__error">{errorMessage}</p>}
        </>
      );
    }

    if (rawValue && typeof rawValue === "object") {
      const textValue = draft ?? JSON.stringify(rawValue, null, 2);
      return (
        <>
          <textarea
            className="settings-field__textarea"
            value={textValue}
            onChange={(event) => handleJsonFieldChange(path, event.target.value)}
            disabled={disabled}
            rows={4}
            spellCheck={false}
          />
          {errorMessage && <p className="settings-field__error">{errorMessage}</p>}
        </>
      );
    }

    const valueString =
      draft ?? (rawValue === null || rawValue === undefined ? "" : String(rawValue as Primitive));
    return (
      <>
        <input
          className="settings-field__input"
          type="text"
          value={valueString}
          onChange={(event) => handleStringChange(path, event.target.value)}
          disabled={disabled}
        />
        {errorMessage && <p className="settings-field__error">{errorMessage}</p>}
      </>
    );
  };

  const disabled = loadingSettings || hiddenFields === null;

  return (
    <section className="page">
      <header className="page__header">
        <div>
          <h2>Bot Control</h2>
          <p>Runs deployen, pausieren und Settings verwalten.</p>
        </div>
        <div style={{ display: "flex", gap: "0.5rem" }}>
          <button type="button" className="button button--ghost" onClick={handleRefresh}>
            Status aktualisieren
          </button>
          <button type="button" className="button button--ghost" onClick={handleRefreshSettings}>
            Settings neu laden
          </button>
        </div>
      </header>

      {message && <div className="card card--success">{message}</div>}
      {error && <div className="card card--error">{error}</div>}

      <div className="grid grid--cols-2 grid--gap-lg">
        <form className="card form" onSubmit={handleDeploy}>
          <h3>Deploy neues Template</h3>
          <label className="form__label">
            Template-Name (optional)
            <input
              type="text"
              value={templateName}
              onChange={(event) => setTemplateName(event.target.value)}
              placeholder="Night Momentum 2025-10-15"
            />
          </label>
          <label className="form__label">
            Beschreibung
            <input
              type="text"
              value={templateDescription}
              onChange={(event) => setTemplateDescription(event.target.value)}
              placeholder="Kurze Beschreibung"
            />
          </label>
          <label className="form__label">
            Tags (comma separated)
            <input
              type="text"
              value={templateTags}
              onChange={(event) => setTemplateTags(event.target.value)}
              placeholder="night, experiment"
            />
          </label>

          <div className="settings-form">
            <div className="settings-form__header">
              <h4>Einstellungen</h4>
              <div className="settings-form__controls">
                <input
                  type="text"
                  className="settings-form__search"
                  placeholder="Parameter suchen..."
                  value={filterSearch}
                  onChange={(event) => setFilterSearch(event.target.value)}
                />
                <span className="settings-form__count">{visibleFieldCount} Felder</span>
              </div>
            </div>

            {loadingSettings || hiddenFields === null ? (
              <p>Lade aktuelle Settings...</p>
            ) : (
              <>
                <div className="settings-table-wrapper">
                  <table className="table settings-table">
                    <thead>
                      <tr>
                        <th>Parameter</th>
                        <th>Wert</th>
                      </tr>
                    </thead>
                    <tbody>
                      {filteredFields.map((field) => (
                        <tr key={field.path}>
                          <td className="settings-table__label-cell">
                            <div className="settings-table__label">
                              <strong>{field.displayName}</strong>
                              <code className="settings-table__path">{field.path}</code>
                            </div>
                            {field.tooltip && <p className="settings-table__hint">{field.tooltip}</p>}
                          </td>
                          <td className="settings-table__value-cell">{buildFieldInput(field.path, disabled)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
                {filteredFields.length === 0 && (
                  <p className="settings-table__empty">Keine Felder passend zum Filter.</p>
                )}
                <details className="settings-raw">
                  <summary>Raw JSON (optional)</summary>
                  <textarea
                    className="settings-field__textarea"
                    value={settingsJson}
                    onChange={(event) => handleRawJsonChange(event.target.value)}
                    rows={10}
                    spellCheck={false}
                  />
                  {jsonError && <p className="settings-field__error">{jsonError}</p>}
                </details>
              </>
            )}
          </div>

          <label className="form__label form__label--row">
            <input
              type="checkbox"
              checked={saveTemplate}
              onChange={(event) => setSaveTemplate(event.target.checked)}
            />
            <span>Template automatisch speichern</span>
          </label>
          <div className="form__actions">
            <button type="submit" className="button" disabled={deploying}>
              {deploying ? "Deploy läuft..." : "Deploy auslösen"}
            </button>
          </div>
        </form>

        <div className="card">
          <h3>Templates</h3>
          {loadingTemplates ? (
            <p>Lade Templates...</p>
          ) : templates.length === 0 ? (
            <p>Noch keine Templates gespeichert.</p>
          ) : (
            <ul className="template-list">
              {templates.map((tpl) => (
                <li key={tpl.id}>
                  <div className="template-list__info">
                    <strong>{tpl.name}</strong>
                    {tpl.description && <span>{tpl.description}</span>}
                    {(tpl.tags ?? []).length > 0 && (
                      <span className="template-list__tags">{tpl.tags.join(", ")}</span>
                    )}
                  </div>
                  <div className="template-list__actions">
                    <button
                      type="button"
                      className="button button--ghost"
                      onClick={() => handleApplyTemplate(tpl.id)}
                    >
                      Laden
                    </button>
                    <button
                      type="button"
                      className="button button--ghost"
                      onClick={() => handleDeleteTemplate(tpl.id)}
                    >
                      Löschen
                    </button>
                  </div>
                </li>
              ))}
            </ul>
          )}
        </div>
      </div>
    </section>
  );
}
