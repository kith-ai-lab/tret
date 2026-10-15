/** Catches a render-time throw below it so one broken view cannot blank the app.
 *
 *  React only catches errors thrown while rendering, in lifecycle methods and in
 *  constructors of the tree underneath. Event handlers and async work (a failed
 *  fetch, a rejected mutation) are not covered and keep surfacing through their
 *  own `error-text` paths. "Try again" clears the caught error and re-renders
 *  the children; if the cause is still there it simply lands back here. Routed
 *  views also pass a `key` of the current path, so navigating away resets the
 *  boundary without a click. */
import { Component, type ErrorInfo, type ReactNode } from 'react'

interface Props {
  children: ReactNode
}

interface State {
  error: Error | null
}

export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null }

  static getDerivedStateFromError(error: unknown): State {
    return { error: error instanceof Error ? error : new Error(String(error)) }
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    // The component stack is what actually locates the crash; the fallback only
    // shows the message, so keep the full detail in the console.
    console.error('ErrorBoundary caught a render error', error, info.componentStack)
  }

  private reset = () => this.setState({ error: null })

  render() {
    const { error } = this.state
    if (!error) return this.props.children
    return (
      <div className="panel" role="alert" style={{ margin: 24 }}>
        <div className="mono-label" style={{ marginBottom: 10 }}>
          This page hit an error
        </div>
        <pre className="code-block" style={{ maxHeight: 240 }}>
          {error.message || String(error)}
        </pre>
        <div className="row" style={{ gap: 8, marginTop: 12 }}>
          <button type="button" className="btn btn-primary" onClick={this.reset}>
            Try again
          </button>
          <button type="button" className="btn" onClick={() => window.location.reload()}>
            Reload
          </button>
        </div>
      </div>
    )
  }
}
