// Copy immutable release code out of the signed app before any yard invocation.
import { cpSync, existsSync, mkdirSync, mkdtempSync, readFileSync, renameSync, rmSync, writeFileSync } from 'node:fs';
import path from 'node:path';
export function ensureCodeCopy({ resourcesPath, appState, version }) {
  if (!/^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][a-zA-Z0-9.+-]+)?$/.test(version)) throw new Error('Invalid desktop version');
  const source = path.join(resourcesPath, 'loopyard');
  if (!existsSync(path.join(source, 'bin/yard'))) return '';
  if (!appState) throw new Error('App-data state directory is required for bundled code');
  const parent = path.join(appState, 'code');
  const target = path.join(parent, version);
  const verify = root => {
    if (JSON.parse(readFileSync(path.join(root, 'BUNDLE.json'), 'utf8')).version !== version) throw new Error('Desktop and origin bundle versions differ');
    if (!existsSync(path.join(root, 'bin/yard')) || !existsSync(path.join(root, 'DESKTOP_MANAGED'))) throw new Error('Incomplete desktop code copy');
  };
  if (existsSync(target)) { verify(target); return target; }
  mkdirSync(parent, {recursive:true});
  const temp = mkdtempSync(path.join(parent, '.tmp-'));
  try {
    cpSync(source, temp, {recursive:true, verbatimSymlinks:true});
    writeFileSync(path.join(temp, 'DESKTOP_MANAGED'), 'Managed by the Loopyard desktop app. Download a new DMG to update.\n');
    verify(temp);
    try { renameSync(temp, target); } catch (error) {
      if (!existsSync(target)) throw error;
      verify(target); // another app launch won the atomic rename
    }
    return target;
  } finally { rmSync(temp, {recursive:true, force:true}); }
}
