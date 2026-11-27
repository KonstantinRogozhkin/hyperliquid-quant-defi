import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import { GLOSSARY, type GlossaryKey } from "./glossary";

export type Lang = "en" | "ru";

const STORAGE_KEY = "hl_desk_lang";

interface LangContextValue {
  lang: Lang;
  setLang: (lang: Lang) => void;
}

// Default value keeps components usable outside the provider (e.g. isolated chart tests) in English.
const LangContext = createContext<LangContextValue>({ lang: "en", setLang: () => undefined });

function detectInitialLang(): Lang {
  try {
    const saved = window.localStorage.getItem(STORAGE_KEY);
    if (saved === "en" || saved === "ru") return saved;
  } catch {
    // Storage may be blocked (private mode, sandboxed iframe) — fall through to browser language.
  }
  const nav = typeof navigator !== "undefined" ? navigator.language || "" : "";
  return nav.toLowerCase().startsWith("ru") ? "ru" : "en";
}

export function LangProvider({ children }: { children: ReactNode }) {
  const [lang, setLangState] = useState<Lang>(detectInitialLang);

  const setLang = useCallback((next: Lang) => {
    setLangState(next);
    try {
      window.localStorage.setItem(STORAGE_KEY, next);
    } catch {
      // Non-persistent preference is fine.
    }
  }, []);

  useEffect(() => {
    document.documentElement.lang = lang;
  }, [lang]);

  const value = useMemo(() => ({ lang, setLang }), [lang, setLang]);
  return <LangContext.Provider value={value}>{children}</LangContext.Provider>;
}

export function useLang() {
  const { lang, setLang } = useContext(LangContext);
  /** Pick the string for the active language. */
  const t = useCallback((en: string, ru: string) => (lang === "ru" ? ru : en), [lang]);
  /** Count-aware noun: English one/many, Russian one/few/many (1 тик, 3 тика, 5 тиков). */
  const p = useCallback(
    (n: number, enOne: string, enMany: string, ruOne: string, ruFew: string, ruMany: string) => {
      if (lang !== "ru") return Math.abs(n) === 1 ? enOne : enMany;
      return ruPlural(n, ruOne, ruFew, ruMany);
    },
    [lang],
  );
  /** Label + plain-language tooltip for a dashboard block or table column. */
  const g = useCallback(
    (key: GlossaryKey) => {
      const entry = GLOSSARY[key];
      return lang === "ru" ? { label: entry.ru, tip: entry.tipRu } : { label: entry.en, tip: entry.tipEn };
    },
    [lang],
  );
  return { lang, setLang, t, p, g };
}

export function ruPlural(n: number, one: string, few: string, many: string): string {
  const mod100 = Math.abs(Math.trunc(n)) % 100;
  const mod10 = mod100 % 10;
  if (mod100 >= 11 && mod100 <= 14) return many;
  if (mod10 === 1) return one;
  if (mod10 >= 2 && mod10 <= 4) return few;
  return many;
}

export function LangToggle() {
  const { lang, setLang } = useLang();
  return (
    <div className="segmented lang-toggle" role="group" aria-label="Language / Язык">
      {(["ru", "en"] as const).map((code) => (
        <button
          key={code}
          type="button"
          className={`btn btn-sm ${lang === code ? "tab-active" : ""}`}
          aria-pressed={lang === code}
          onClick={() => setLang(code)}
        >
          {code.toUpperCase()}
        </button>
      ))}
    </div>
  );
}
