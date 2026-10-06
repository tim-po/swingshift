// The "AI tools" line of a computer row, in plain words. A remote computer's
// tools are checked ON that computer (capabilities.probe over its Loopyard
// connection); when it can't be reached we say "Not checked", never "None found".
import { cliName, cliSignInLabel, type OriginCapsRecord, type OriginCliCapability } from '@loopyard/api';

/** Checked on the remote computer itself, not guessed by this one. */
export const capsCheckedRemotely = (caps?: OriginCapsRecord) =>
  !!caps && (caps.via === 'origin-channel' || caps.cliCapabilities.some((c) => c.remote));

/** Tooltip for the tools summary: where the check ran. */
export function capsSourceTip(caps: OriginCapsRecord | undefined, local: boolean): string {
  if (local) return 'AI tools found on this computer';
  return capsCheckedRemotely(caps) ? 'AI tools checked on that computer over its Swingshift connection' : 'AI tools recorded for that computer';
}

/** What the tools line says when no tool is listed: an honest empty vs. an unchecked computer. */
export function capsEmptyLabel(caps: OriginCapsRecord, local: boolean): { text: string; tip?: string } {
  if (!caps.ok) return { text: 'Not checked', tip: caps.reason || 'This computer could not be asked which AI tools it has' };
  return { text: local ? 'None found' : 'None found on that computer', tip: caps.reason };
}

/** The row's tools summary, same for this computer and a remote one: a signed-in
 *  tool is just its name; any other says so ("Codex (not signed in)"), so a CLI
 *  that is missing or signed out never reads as ready. '' when none is listed. */
export const capsToolsSummary = (list?: OriginCliCapability[] | null) =>
  (list ?? []).map((c) => (c.authed === true ? cliName(c.cli) : `${cliName(c.cli)} (${cliSignInLabel(c)})`)).join(' · ');
