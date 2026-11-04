"use client";

// Sticky app header. Shows the brand and, when signed in, the user's email +
// a logout button. Hidden auth controls on the login page (no token).

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useAuth } from "@/lib/store";

export function Header() {
  const router = useRouter();
  const token = useAuth((s) => s.token);
  const email = useAuth((s) => s.email);
  const hasHydrated = useAuth((s) => s.hasHydrated);
  const logout = useAuth((s) => s.logout);

  return (
    <header className="app-header">
      <div className="bar">
        <Link href={token ? "/agents" : "/"} className="brand" style={{ color: "inherit" }}>
          <span className="logo">◆</span>
          <span>Agent Studio</span>
        </Link>
        {hasHydrated && token && (
          <div style={{ display: "flex", alignItems: "center", gap: 14 }}>
            <span className="faint" title={email ?? ""}>
              {email}
            </span>
            <button
              className="btn ghost btn-sm"
              onClick={() => {
                logout();
                router.replace("/");
              }}
            >
              Log out
            </button>
          </div>
        )}
      </div>
    </header>
  );
}
