import { createContext, useContext } from "react";
import type { Me } from "./types";

export interface Session {
  me: Me;
  /** Re-reads `/auth/me`, after the set of projects changes. */
  refresh: () => Promise<void>;
}

export const SessionContext = createContext<Session | null>(null);

export function useSession(): Session {
  const session = useContext(SessionContext);
  if (!session) throw new Error("useSession is only available below the signed-in app");
  return session;
}

/** Signed-in (hosted) mode, as opposed to the local snapshot with its one implicit user. */
export const isHosted = (me: Me) => me.data_source === "cortico_api";
