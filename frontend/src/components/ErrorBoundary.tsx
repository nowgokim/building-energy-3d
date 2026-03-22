import { Component, type ReactNode } from "react";

interface Props {
  children: ReactNode;
  fallback?: ReactNode;
}

interface State {
  hasError: boolean;
  error: Error | null;
}

export default class ErrorBoundary extends Component<Props, State> {
  state: State = { hasError: false, error: null };

  static getDerivedStateFromError(error: Error): State {
    return { hasError: true, error };
  }

  componentDidCatch(error: Error) {
    console.error("ErrorBoundary caught:", error);
  }

  render() {
    if (this.state.hasError) {
      return (
        this.props.fallback ?? (
          <div
            style={{
              width: "100%",
              height: "100%",
              display: "flex",
              flexDirection: "column",
              alignItems: "center",
              justifyContent: "center",
              background: "#1a1a2e",
              color: "#fff",
              fontFamily: "sans-serif",
              gap: 12,
            }}
          >
            <div style={{ fontSize: 18 }}>3D 뷰어 로드에 실패했습니다</div>
            <div style={{ fontSize: 13, color: "#aaa" }}>
              {this.state.error?.message}
            </div>
            <button
              onClick={() => window.location.reload()}
              style={{
                marginTop: 8,
                padding: "8px 20px",
                background: "#4a9eff",
                color: "#fff",
                border: "none",
                borderRadius: 4,
                cursor: "pointer",
                fontSize: 14,
              }}
            >
              새로고침
            </button>
          </div>
        )
      );
    }
    return this.props.children;
  }
}
