import { createContext, useContext } from 'react'

export const AuthContext = createContext(null)

// { user, signIn, signUp, signOut }; user is null when signed out.
export function useAuth() {
  const auth = useContext(AuthContext)
  if (!auth) {
    throw new Error('useAuth must be used inside <AuthProvider>')
  }
  return auth
}
