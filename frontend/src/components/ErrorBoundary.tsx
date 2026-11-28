import { Component, type ErrorInfo, type ReactNode } from "react";
import { detectInitialLang, type Lang } from "../i18n";

const STRINGS: Record<Lang, { title: string; retry: string }> = {
  en: { title: "Dashboard render error", retry: "Retry" },
  ru: { title: "Ошибка отрисовки дашборда", retry: "Повторить" },
};

/**
 * Top-level render guard. It sits outside LangProvider (the provider itself may be what crashed), so it reads
 * the persisted language choice directly instead of using the React context.
 */
export class ErrorBoundary extends Component<{ children: ReactNode }, { error: Error | null }> {
  state: { error: Error | null } = { error: null };

  static getDerivedStateFromError(error: Error) {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error("UI render error", error, info.componentStack);
  }

  render() {
    if (this.state.error) {
      const s = STRINGS[detectInitialLang()];
      return (
        <div role="alert" style={{ padding: 24 }}>
          <h2>{s.title}</h2>
          <pre>{this.state.error.message}</pre>
          <button type="button" onClick={() => this.setState({ error: null })}>
            {s.retry}
          </button>
        </div>
      );
    }
    return this.props.children;
  }
}
