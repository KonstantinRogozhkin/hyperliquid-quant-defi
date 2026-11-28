import { useCallback, useEffect, useId, useLayoutEffect, useRef, useState, type CSSProperties, type ReactNode } from "react";
import { createPortal } from "react-dom";
import type { GlossaryKey } from "../glossary";
import { useLang } from "../i18n";

const MAX_TIP_WIDTH = 320;
const EDGE_GAP = 8;
const MIN_ABOVE_CLEARANCE = 220;

interface TipPosition {
  left: number;
  top: number;
  above: boolean;
  width: number;
}

/**
 * Hover / focus / tap tooltip. Rendered in a portal with fixed positioning so it is never clipped by
 * the horizontally scrollable `.table-wrap` containers or the top/bottom viewport edges.
 */
export function Tip({
  text,
  children,
  className = "",
  ariaLabel,
  focusable = true,
}: {
  text: string;
  children?: ReactNode;
  className?: string;
  ariaLabel?: string;
  focusable?: boolean;
}) {
  const anchorRef = useRef<HTMLSpanElement>(null);
  const tooltipRef = useRef<HTMLDivElement>(null);
  const lastPointerTypeRef = useRef<string>("mouse");
  const [pos, setPos] = useState<TipPosition | null>(null);
  const id = useId();

  const computePos = useCallback((): TipPosition | null => {
    const el = anchorRef.current;
    if (!el) return null;
    const r = el.getBoundingClientRect();
    const vw = window.innerWidth || document.documentElement.clientWidth || 1024;
    const width = Math.min(MAX_TIP_WIDTH, Math.max(120, vw - 2 * EDGE_GAP));
    const center = r.left + r.width / 2;
    const left = Math.min(Math.max(center, width / 2 + EDGE_GAP), vw - width / 2 - EDGE_GAP);
    const above = r.top > MIN_ABOVE_CLEARANCE;
    const rawTop = above ? r.top - EDGE_GAP : r.bottom + EDGE_GAP;
    return { left, top: Math.max(EDGE_GAP, rawTop), above, width };
  }, []);

  const show = useCallback(() => {
    const next = computePos();
    if (next) setPos(next);
  }, [computePos]);

  const hide = useCallback(() => setPos(null), []);

  // Measure actual rendered tooltip height so tall multi-line (e.g. Russian) tooltips never clip off-screen.
  useLayoutEffect(() => {
    if (!pos || !tooltipRef.current || !anchorRef.current) return;
    const tipHeight = tooltipRef.current.offsetHeight;
    if (tipHeight <= 0) return;
    const r = anchorRef.current.getBoundingClientRect();
    const vh = window.innerHeight || document.documentElement.clientHeight || 768;
    if (pos.above && pos.top - tipHeight < EDGE_GAP) {
      const roomBelow = vh - r.bottom - EDGE_GAP;
      if (roomBelow >= tipHeight || roomBelow > r.top) {
        const nextTop = Math.max(EDGE_GAP, Math.min(r.bottom + EDGE_GAP, vh - tipHeight - EDGE_GAP));
        if (pos.above || Math.abs(pos.top - nextTop) > 1) {
          setPos({ ...pos, above: false, top: nextTop });
        }
      } else {
        const clampedTop = tipHeight + EDGE_GAP;
        if (Math.abs(pos.top - clampedTop) > 1) {
          setPos({ ...pos, top: clampedTop });
        }
      }
    } else if (!pos.above && pos.top + tipHeight > vh - EDGE_GAP) {
      const clampedTop = Math.max(EDGE_GAP, vh - tipHeight - EDGE_GAP);
      if (Math.abs(pos.top - clampedTop) > 1) {
        setPos({ ...pos, top: clampedTop });
      }
    }
  }, [pos, text]);

  useEffect(() => {
    if (!pos) return;
    const onPointerDownOutside = (e: PointerEvent) => {
      if (anchorRef.current && e.target instanceof Node && !anchorRef.current.contains(e.target)) {
        hide();
      }
    };
    window.addEventListener("scroll", hide, true);
    window.addEventListener("resize", hide);
    document.addEventListener("pointerdown", onPointerDownOutside);
    return () => {
      window.removeEventListener("scroll", hide, true);
      window.removeEventListener("resize", hide);
      document.removeEventListener("pointerdown", onPointerDownOutside);
    };
  }, [pos, hide]);

  if (!text) return <>{children}</>;

  return (
    <>
      <span
        ref={anchorRef}
        className={`tip-anchor ${className}`.trim()}
        tabIndex={focusable ? 0 : undefined}
        aria-label={ariaLabel}
        aria-describedby={pos ? id : undefined}
        onPointerDown={(e) => {
          lastPointerTypeRef.current = e.pointerType || "mouse";
        }}
        onClick={() => {
          if (lastPointerTypeRef.current === "touch" || lastPointerTypeRef.current === "pen") {
            setPos((prev) => (prev ? null : computePos()));
          }
        }}
        onMouseEnter={show}
        onMouseLeave={hide}
        onFocus={focusable ? show : undefined}
        onBlur={focusable ? hide : undefined}
        onKeyDown={
          focusable
            ? (e) => {
                if (e.key === "Escape") hide();
              }
            : undefined
        }
      >
        {children}
      </span>
      {pos &&
        createPortal(
          <div
            ref={tooltipRef}
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
  return <Tip text={text} className="tip-icon" ariaLabel={t("What is this?", "Что это?")} focusable />;
}

/** Table header cell with a plain-language explanation on hover/tap (non-focusable to keep keyboard tab order clean). */
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
    <th scope="col" className={num ? "num" : undefined}>
      <Tip text={tip ?? entry?.tip ?? ""} className="th-tip" focusable={false}>
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
