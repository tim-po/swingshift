// Phase 5 owner scope for the loop DETAIL pane. The Loops view provides the picked
// owner ('' = unscoped); detail reads then carry ?owner= so the backend refuses a
// cross-owner read. Unscoped (single-owner box, or "all") → URLs + query keys as master.
import { createContext, useContext } from 'react';

export const LoopOwnerScope = createContext('');
export const useLoopOwnerScope = () => useContext(LoopOwnerScope) || undefined;
/** Suffix for a query key: nothing when unscoped, so cache keys stay identical to master. */
export const ownerKey = (owner?: string) => (owner ? [owner] : []);
