// Zustand store for client-side auth state.
// We persist the JWT to localStorage so a refresh keeps the user signed in.
// React Query owns *server* state (agents, queries); Zustand owns *client*
// state (the token, UI toggles) - a clean separation worth explaining.

import { create } from "zustand";
import { persist } from "zustand/middleware";

interface AuthState {
  token: string | null;
  email: string | null;
  // True once persist has finished reading localStorage. Pages must wait for
  // this before deciding "no token -> redirect to login", otherwise the first
  // render (token still null) would bounce an authenticated user out.
  hasHydrated: boolean;
  // Set when a 401 forced a logout, so the login page can show "session
  // expired". We keep it in the store (not a URL param) so it survives the
  // redirect races between the global handler and per-page auth guards.
  sessionExpired: boolean;
  setAuth: (token: string, email: string) => void;
  logout: () => void;
  expireSession: () => void;
  clearExpired: () => void;
  setHasHydrated: (v: boolean) => void;
}

export const useAuth = create<AuthState>()(
  persist(
    (set) => ({
      token: null,
      email: null,
      hasHydrated: false,
      sessionExpired: false,
      setAuth: (token, email) =>
        set({ token, email, sessionExpired: false }),
      logout: () => set({ token: null, email: null }),
      expireSession: () =>
        set({ token: null, email: null, sessionExpired: true }),
      clearExpired: () => set({ sessionExpired: false }),
      setHasHydrated: (v) => set({ hasHydrated: v }),
    }),
    {
      name: "agent-studio-auth",
      // Only persist the actual credentials, never the hydration flag.
      partialize: (state) => ({ token: state.token, email: state.email }),
      onRehydrateStorage: () => (state) => {
        // Runs after localStorage has been read back into the store.
        state?.setHasHydrated(true);
      },
    }
  )
);
