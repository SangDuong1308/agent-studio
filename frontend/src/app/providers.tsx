"use client";

// React Query provider. Wraps the app so any client component can use
// useQuery/useMutation with a shared cache.
//
// It also owns global auth-expiry handling: when any authenticated API call
// returns 401 (token expired/invalid), we clear the session and bounce the
// user to the login screen with a "session expired" notice - rather than
// letting each page show a misleading "failed to load" error.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import { useEffect, useState, type ReactNode } from "react";
import { ApiError, setUnauthorizedHandler } from "@/lib/api";
import { useAuth } from "@/lib/store";

export function Providers({ children }: { children: ReactNode }) {
  const router = useRouter();
  const expireSession = useAuth((s) => s.expireSession);

  const [client] = useState(
    () =>
      new QueryClient({
        defaultOptions: {
          queries: {
            staleTime: 5_000,
            // Never retry auth failures - retrying an expired token is pointless.
            retry: (failureCount, error) => {
              if (error instanceof ApiError && error.status === 401) return false;
              return failureCount < 1;
            },
          },
        },
      })
  );

  useEffect(() => {
    // Register the global 401 handler once. On expiry: flag the session as
    // expired (clears the token) and redirect to login. The expired flag lives
    // in the store so the login page can show a message even if a per-page
    // auth guard also fires a redirect.
    setUnauthorizedHandler(() => {
      expireSession();
      router.replace("/");
    });
    return () => setUnauthorizedHandler(null);
  }, [expireSession, router]);

  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}
