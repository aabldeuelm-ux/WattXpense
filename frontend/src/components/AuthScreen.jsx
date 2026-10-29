import { useState } from 'react'

import api from '../api'

/**
 * The signed-out screen.
 *
 * WattXpense holds a household's usage history, so the dashboard is private by default:
 * there is no anonymous read-only view. Registration can be switched off by the operator,
 * in which case only sign-in is offered.
 */
export default function AuthScreen({ registrationEnabled = true, initialMode = 'login', onSuccess }) {
  const [mode, setMode] = useState(registrationEnabled ? initialMode : 'login')
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [displayName, setDisplayName] = useState('')
  const [error, setError] = useState(null)
  const [busy, setBusy] = useState(false)

  const isRegister = mode === 'register' && registrationEnabled

  async function submit(event) {
    event.preventDefault()
    setBusy(true)
    setError(null)
    try {
      const payload = isRegister
        ? { email, password, display_name: displayName || null }
        : { email, password }
      const result = isRegister ? await api.register(payload) : await api.login(payload)
      onSuccess(result.user)
    } catch (err) {
      setError(err)
    } finally {
      setBusy(false)
    }
  }

  return (
    <main className="auth-shell">
      <section className="auth-hero">
        <div className="brand-mark" aria-hidden="true">
          ⚡
        </div>
        <p className="eyebrow">Private household energy</p>
        <h1>WattXpense</h1>
        <p className="auth-lede">
          Readings, tariffs, forecasts and bills in one place — with every estimate labelled and
          every account kept separate.
        </p>

        <ul className="auth-points">
          <li>
            <strong>Your data stays yours.</strong> Devices, readings, tariffs and forecasts are
            scoped to your account.
          </li>
          <li>
            <strong>Simulated is always labelled.</strong> The bundled simulator is never presented
            as a real meter.
          </li>
          <li>
            <strong>No invented measurements.</strong> A current sensor alone cannot produce real
            power, so derived figures carry an estimate flag.
          </li>
        </ul>

        <p className="auth-footnote">
          There is no physical meter attached to this project. Hardware support is documented, not
          tested on a live installation.
        </p>
      </section>

      <section className="auth-card" aria-labelledby="auth-title">
        <div className="auth-tabs" role="tablist" aria-label="Account access">
          <button
            type="button"
            role="tab"
            aria-selected={!isRegister}
            className={!isRegister ? 'active' : ''}
            onClick={() => {
              setMode('login')
              setError(null)
            }}
          >
            Sign in
          </button>
          {registrationEnabled ? (
            <button
              type="button"
              role="tab"
              aria-selected={isRegister}
              className={isRegister ? 'active' : ''}
              onClick={() => {
                setMode('register')
                setError(null)
              }}
            >
              Create account
            </button>
          ) : null}
        </div>

        <h2 id="auth-title">{isRegister ? 'Create your account' : 'Welcome back'}</h2>
        <p className="panel-note">
          {isRegister
            ? 'Use an email and a password of at least 8 characters. You will be signed in immediately.'
            : 'Sign in to see your own devices and usage.'}
        </p>

        <form onSubmit={submit} className="auth-form">
          {isRegister ? (
            <label className="field">
              <span className="field-label">Display name (optional)</span>
              <input
                autoComplete="name"
                value={displayName}
                onChange={(event) => setDisplayName(event.target.value)}
                maxLength={120}
              />
            </label>
          ) : null}

          <label className="field">
            <span className="field-label">Email</span>
            <input
              type="email"
              autoComplete="email"
              required
              value={email}
              onChange={(event) => setEmail(event.target.value)}
              placeholder="you@example.com"
            />
          </label>

          <label className="field">
            <span className="field-label">Password</span>
            <input
              type="password"
              autoComplete={isRegister ? 'new-password' : 'current-password'}
              required
              minLength={isRegister ? 8 : 1}
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              placeholder={isRegister ? 'At least 8 characters' : 'Your password'}
            />
          </label>

          {error ? (
            <div className="error" role="alert">
              {error.message}
            </div>
          ) : null}

          <button className="primary" type="submit" disabled={busy}>
            {busy ? 'Please wait…' : isRegister ? 'Create account and sign in' : 'Sign in'}
          </button>
        </form>

        {registrationEnabled ? (
          <p className="auth-switch">
            {isRegister ? 'Already have an account?' : 'New here?'}{' '}
            <button
              type="button"
              className="link-button"
              onClick={() => {
                setMode(isRegister ? 'login' : 'register')
                setError(null)
              }}
            >
              {isRegister ? 'Sign in instead' : 'Create an account'}
            </button>
          </p>
        ) : (
          <p className="auth-switch muted">Registration is disabled on this installation.</p>
        )}

        <p className="auth-security">
          Sessions use an HttpOnly cookie; state-changing requests also require a CSRF token.
        </p>
      </section>
    </main>
  )
}
