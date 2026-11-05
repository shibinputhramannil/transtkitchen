import { useCallback, useEffect, useMemo, useState } from 'react'
import * as api from '../api/client.js'
import { clearTokens, getTokens, setTokens, subscribe } from './tokens.js'
import { AuthContext } from './useAuth.js'

export function AuthProvider({ children }) {
  const [user, setUser] = useState(null)

  // Tokens are cleared on sign-out and when a refresh fails; either way the
  // user is signed out with them.
  useEffect(
    () =>
      subscribe((tokens) => {
        if (!tokens) {
          setUser(null)
        }
      }),
    [],
  )

  const signIn = useCallback(async (email, password) => {
    setTokens(await api.login(email, password))
    try {
      setUser(await api.getMe())
    } catch (error) {
      clearTokens()
      throw error
    }
  }, [])

  const signUp = useCallback(
    async (account) => {
      await api.register(account)
      await signIn(account.email, account.password)
    },
    [signIn],
  )

  const signOut = useCallback(async () => {
    const tokens = getTokens()
    clearTokens()
    if (tokens) {
      // Revoke the refresh token too. The user is already signed out locally,
      // so a failure here (for example, a token that already expired) is fine.
      await api.logout(tokens.refresh).catch(() => {})
    }
  }, [])

  const updateProfile = useCallback(async (changes) => {
    setUser(await api.updateMe(changes))
  }, [])

  // The server revokes every earlier token; keep this tab signed in with the
  // fresh pair it returns (still in memory only).
  const changePassword = useCallback(async (currentPassword, newPassword) => {
    setTokens(await api.changePassword(currentPassword, newPassword))
  }, [])

  const value = useMemo(
    () => ({ user, signIn, signUp, signOut, updateProfile, changePassword }),
    [user, signIn, signUp, signOut, updateProfile, changePassword],
  )

  return <AuthContext value={value}>{children}</AuthContext>
}
