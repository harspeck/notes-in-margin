import { useCallback, useRef, useState } from "react";
import type { ReactNode } from "react";
import { ToastContext } from "./format";
import type { Provenance, ToastAction } from "./format";

const TOAST_MS = 3600;
const TOAST_WITH_ACTION_MS = 9000;

export function ToastProvider({ children }: { children: ReactNode }) {
  const [text, setText] = useState("");
  const [action, setAction] = useState<ToastAction | null>(null);
  const [on, setOn] = useState(false);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const toast = useCallback((t: string, a?: ToastAction) => {
    setText(t);
    setAction(a ?? null);
    setOn(true);
    if (timer.current) clearTimeout(timer.current);
    timer.current = setTimeout(() => setOn(false), a ? TOAST_WITH_ACTION_MS : TOAST_MS);
  }, []);
  return (
    <ToastContext.Provider value={toast}>
      {children}
      <div className={"toast" + (on ? " on" : "")} role="status">
        {text}
        {on && action && (
          <button
            onClick={() => {
              setOn(false);
              action.run();
            }}
          >
            {action.label}
          </button>
        )}
      </div>
    </ToastContext.Provider>
  );
}

const ICONS = {
  star: <polygon points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2" />,
  play: <polygon points="6 3 20 12 6 21 6 3" />,
  sparkle: (
    <>
      <path d="M10 3 11.9 8.6 17.5 10.5 11.9 12.4 10 18 8.1 12.4 2.5 10.5 8.1 8.6Z" />
      <path d="M19 14 19.8 16.2 22 17 19.8 17.8 19 20 18.2 17.8 16 17 18.2 16.2Z" />
    </>
  ),
  pause: (
    <>
      <rect x="6" y="4" width="4" height="16" />
      <rect x="14" y="4" width="4" height="16" />
    </>
  ),
  x: (
    <>
      <path d="M18 6 6 18" />
      <path d="m6 6 12 12" />
    </>
  ),
  mic: (
    <>
      <rect x="9" y="2" width="6" height="12" rx="3" />
      <path d="M5 11a7 7 0 0 0 14 0" />
      <path d="M12 18v4" />
    </>
  ),
};

export function Icon({ name, fill }: { name: keyof typeof ICONS; fill?: boolean }) {
  return (
    <svg className={"i" + (fill ? " fill" : "")} viewBox="0 0 24 24" aria-hidden="true">
      {ICONS[name]}
    </svg>
  );
}

/** The one sign that something uses AI: on a control it means pressing it asks the AI, on
 * content it means the AI wrote it. */
export function AiMark({ label = "Uses AI" }: { label?: string }) {
  return (
    <span className="aimark" role="img" aria-label={label} title={label}>
      <Icon name="sparkle" fill />
    </span>
  );
}

const MAX_DOTS = 24;

/** One dot per conversation the user has opened; filled ones are where the question came up. */
export function Dots({ heard, total }: { heard: number; total: number }) {
  if (total === 0 || total > MAX_DOTS) return null;
  return (
    <span className="dots" aria-hidden="true">
      {Array.from({ length: total }, (_, i) => (
        <i key={i} className={i < heard ? "on" : ""} />
      ))}
    </span>
  );
}

export function ProvTag({ prov }: { prov: Provenance }) {
  if (prov === "ai") return <span className="tag ai">Suggested by AI</span>;
  if (prov === "kept") return <span className="tag kept">AI, kept as written</span>;
  if (prov === "both") return <span className="tag both">AI + you</span>;
  return <span className="tag yours">Yours</span>;
}
