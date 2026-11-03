"use client";

import { createContext, useContext } from "react";
import type { Account, Permission } from "../lib/api";

/**
 * The signed-in person, provided by AuthShell. Pages use `can()` only to hide
 * or explain actions a role cannot take; the server enforces every permission.
 */
export const AccountContext = createContext<Account | null>(null);

export function useAccount(): { account: Account | null; can: (permission: Permission) => boolean } {
  const account = useContext(AccountContext);
  return {
    account,
    // Missing permissions mean no access (fail closed), never a crash.
    can: (permission) => Boolean(account?.permissions?.includes(permission)),
  };
}
