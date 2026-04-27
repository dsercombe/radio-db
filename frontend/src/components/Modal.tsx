import { ReactNode } from "react";

interface ModalProps {
  title: string;
  open: boolean;
  onClose: () => void;
  children: ReactNode;
}

export function Modal({ title, open, onClose, children }: ModalProps): JSX.Element | null {
  if (!open) return null;
  return (
    <div className="modal-overlay" role="dialog" aria-modal="true">
      <div className="modal">
        <header className="modal__header">
          <h3>{title}</h3>
          <button type="button" className="modal__close" onClick={onClose}>
            ×
          </button>
        </header>
        <div className="modal__content">{children}</div>
      </div>
    </div>
  );
}
