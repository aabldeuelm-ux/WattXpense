import { useState } from 'react'

import api from '../api'
import { ErrorBox, Field, Panel } from './ui'

export default function AccountPanel({ user, meta, onSignedOut }) {
  const [currentPassword, setCurrentPassword] = useState('')
  const [newPassword, setNewPassword] = useState('')
  const [confirmPassword, setConfirmPassword] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)
  const [message, setMessage] = useState(null)

  async function changePassword(event) {
    event.preventDefault()
    setError(null)
    setMessage(null)
    if (newPassword !== confirmPassword) {
      setError(new Error('The new passwords do not match'))
      return
    }
    setBusy(true)
    try {
      await api.changePassword({ current_password: currentPassword, new_password: newPassword })
      setCurrentPassword('')
      setNewPassword('')
      setConfirmPassword('')
      setMessage('Password changed. Other sessions have been signed out.')
    } catch (err) {
      setError(err)
    } finally {
      setBusy(false)
    }
  }

  async function signOut() {
    setBusy(true)
    try {
      await api.logout()
    } catch {
      /* Even if the request fails, the local screen should return to sign-in. */
    } finally {
      setBusy(false)
      onSignedOut()
    }
  }

  return (
    <>
      <Panel title="Account" note="This dashboard only ever shows data owned by the signed-in account.">
        <dl className="account-facts">
          <div>
            <dt>Email</dt>
            <dd>{user.email}</dd>
          </div>
          <div>
            <dt>Display name</dt>
            <dd>{user.display_name || 'not set'}</dd>
          </div>
          <div>
            <dt>Account created</dt>
            <dd>{user.created_at ? new Date(user.created_at).toLocaleString() : 'unknown'}</dd>
          </div>
          <div>
            <dt>Last sign-in</dt>
            <dd>{user.last_login_at ? new Date(user.last_login_at).toLocaleString() : 'unknown'}</dd>
          </div>
          <div>
            <dt>Timezone</dt>
            <dd>{meta?.timezone || 'unknown'}</dd>
          </div>
          <div>
            <dt>Registration</dt>
            <dd>{meta?.registration_enabled ? 'open on this installation' : 'closed'}</dd>
          </div>
        </dl>
        <div className="button-row">
          <button type="button" className="action" onClick={signOut} disabled={busy}>
            Sign out
          </button>
        </div>
      </Panel>

      <Panel
        title="Change password"
        note="Changing the password signs out every other session. This browser stays signed in."
      >
        <form onSubmit={changePassword} className="password-form">
          <Field label="Current password">
            <input
              type="password"
              autoComplete="current-password"
              required
              value={currentPassword}
              onChange={(event) => setCurrentPassword(event.target.value)}
            />
          </Field>
          <Field label="New password" hint="At least 8 characters.">
            <input
              type="password"
              autoComplete="new-password"
              required
              minLength={8}
              value={newPassword}
              onChange={(event) => setNewPassword(event.target.value)}
            />
          </Field>
          <Field label="Confirm new password">
            <input
              type="password"
              autoComplete="new-password"
              required
              minLength={8}
              value={confirmPassword}
              onChange={(event) => setConfirmPassword(event.target.value)}
            />
          </Field>
          <ErrorBox error={error} />
          {message ? <p className="success small">{message}</p> : null}
          <div className="button-row">
            <button className="primary" type="submit" disabled={busy}>
              {busy ? 'Saving…' : 'Change password'}
            </button>
          </div>
        </form>
      </Panel>

      <Panel title="Security notes">
        <ul className="notes">
          <li>Passwords are stored as salted scrypt hashes, never as plain text.</li>
          <li>Session cookies are HttpOnly and SameSite=Lax; only a hash of the session token is stored.</li>
          <li>Cookie-authenticated writes must include a CSRF token from the readable CSRF cookie.</li>
          <li>Login attempts are rate-limited per address and locked per account after repeated failures.</li>
          <li>Device tokens are separate from user sessions and are shown once when issued.</li>
        </ul>
      </Panel>
    </>
  )
}
