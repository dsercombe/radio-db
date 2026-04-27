import type { ReactNode } from "react";

interface ConfirmDialogProps {
  open: boolean;
  title: string;
  description?: ReactNode;
  confirmLabel?: string;
  cancelLabel?: string;
  onConfirm: () => void;
  onCancel: () => void;
}

export function ConfirmDialog({
  open,
  title,
  description,
  confirmLabel = "Löschen",
  cancelLabel = "Abbrechen",
  onConfirm,
  onCancel
}: ConfirmDialogProps): JSX.Element | null {
  if (!open) {
    return null;
  }
  return (
    <div className="modal-overlay">
      <div className="modal">
        <div className="modal__header">
          <h3>{title}</h3>
        </div>
        <div className="modal__content">
          {description ? description : <p>Möchtest du wirklich fortfahren?</p>}
        </div>
        <div className="modal__actions">
          <button type="button" className="button button--ghost" onClick={onCancel}>
            {cancelLabel}
          </button>
          <button type="button" className="button button--danger" onClick={onConfirm}>
            {confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}
