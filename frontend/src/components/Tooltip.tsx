import { useCallback, useEffect, useId, useRef, useState, type CSSProperties, type ReactNode } from "react";
import { createPortal } from "react-dom";
import type { GlossaryKey } from "../glossary";
import { useLang } from "../i18n";

const MAX_TIP_WIDTH = 320;
const EDGE_GAP = 8;

interface TipPosition {
  left: number;
  top: number;
  above: boolean;
  width: number;
}

/**
 * Hover / focus / tap tooltip. Rendered in a portal with fixed positioning so it is never clipped by
 * the horizontally scrollable `.table-wrap` containers.
 */
export function Tip({
  text,
  children,
  className = "",
  ariaLabel,
}: {
  text: string;
  children?: ReactNode;
  className?: string;
  ariaLabel?: string;
}) {
  const anchorRef = useRef<HTMLSpanElement>(null);
  const [pos, setPos] = useState<TipPosition | null>(null);
  const id = useId();

  const show = useCallback(() => {
    const el = anchorRef.current;
    if (!el) return;
    const r = el.getBoundingClientRect();
    const vw = window.innerWidth || document.documentElement.clientWidth || 1024;
    const width = Math.min(MAX_TIP_WIDTH, vw - 2 * EDGE_GAP);
    const center = r.left + r.width / 2;
    const left = Math.min(Math.max(center, width / 2 + EDGE_GAP), vw - width / 2 - EDGE_GAP);
    const above = r.top > 160;
    setPos({ left, top: above ? r.top - EDGE_GAP : r.bottom + EDGE_GAP, above, width });
  }, []);

  const hide = useCallback(() => setPos(null), []);

  useEffect(() => {
    if (!pos) return;
    window.addEventListener("scroll", hide, true);
    window.addEventListener("resize", hide);
    return () => {
      window.removeEventListener("scroll", hide, true);
      window.removeEventListener("resize", hide);
    };
  }, [pos, hide]);

  if (!text) return <>{children}</>;

  return (
    <>
      <span
        ref={anchorRef}
        className={`tip-anchor ${className}`.trim()}
        tabIndex={0}
        aria-label={ariaLabel}
        aria-describedby={pos ? id : undefined}
        onMouseEnter={show}
        onMouseLeave={hide}
        onFocus={show}
        onBlur={hide}
        onKeyDown={(e) => {
          if (e.key === "Escape") hide();
        }}
      >
        {children}
      </span>
      {pos &&
        createPortal(
          <div
            role="tooltip"
            id={id}
            className="tooltip"
            style={{
              left: pos.left,
              top: pos.top,
              maxWidth: pos.width,
              transform: pos.above ? "translate(-50%, -100%)" : "translate(-50%, 0)",
            }}
          >
            {text}
          </div>,
          document.body,
        )}
    </>
  );
}

/** Small circled "i" icon that explains the block it sits next to. */
export function InfoTip({ text }: { text: string }) {
  const { t } = useLang();
  return <Tip text={text} className="tip-icon" ariaLabel={t("What is this?", "Что это?")} />;
}

/** Table header cell with a plain-language explanation on hover. */
export function Th({
  k,
  num = false,
  label,
  tip,
}: {
  k?: GlossaryKey;
  num?: boolean;
  label?: ReactNode;
  tip?: string;
}) {
  const { g } = useLang();
  const entry = k ? g(k) : null;
  return (
    <th className={num ? "num" : undefined}>
      <Tip text={tip ?? entry?.tip ?? ""} className="th-tip">
        {label ?? entry?.label}
      </Tip>
    </th>
  );
}

/** Section `<h2>` title with an info icon. `suffix` is appended to the translated label. */
export function SectionTitle({ k, suffix, label }: { k: GlossaryKey; suffix?: ReactNode; label?: ReactNode }) {
  const { g } = useLang();
  const entry = g(k);
  return (
    <h2 className="section-title">
      {label ?? entry.label}
      {suffix}
      <InfoTip text={entry.tip} />
    </h2>
  );
}

/** KPI card label with an info icon. */
export function KpiLabel({ k, suffix, style }: { k: GlossaryKey; suffix?: ReactNode; style?: CSSProperties }) {
  const { g } = useLang();
  const entry = g(k);
  return (
    <div className="kpi-label" style={style}>
      {entry.label}
      {suffix}
      <InfoTip text={entry.tip} />
    </div>
  );
}
