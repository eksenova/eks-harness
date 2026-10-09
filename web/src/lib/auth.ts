import { createContext, useContext } from "react";
import type { AccessLevel, MeResponse } from "../api/types";

export interface AuthInfo {
  me: MeResponse;
  isAdmin: boolean;
  authEnabled: boolean;
  username: string;
}

export const AuthContext = createContext<AuthInfo | null>(null);

export function useAuth(): AuthInfo {
  const value = useContext(AuthContext);
  if (!value) throw new Error("useAuth outside the signed-in shell");
  return value;
}

export function canEdit(access: AccessLevel | null | undefined, auth: AuthInfo): boolean {
  if (auth.isAdmin) return true;
  return access === "editor" || access === "admin";
}
