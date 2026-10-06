// Stage the origin bundle INTO the .app (PHASE-B-SPEC §1.9 item 1, P9). Run by
// `dist:mac` before electron-builder. For each electron-builder mac arch it:
//   1. verifies <repo>/dist/loopyard-origin-<slug>.tar.gz against its .sha256
//      (the first field — same rule as install.sh, never a verbatim `sha256sum -c`);
//   2. rm -rf <repo>/dist/origin-mac-<arch>/, then unpacks the tarball there with
//      --strip-components=1 (drops the tarball's `loopyard/` top dir), so
//      bin/yard sits at dist/origin-mac-<arch>/bin/yard.
// electron-builder.yml's mac.extraResources copies dist/origin-mac-${arch} to
// Contents/Resources/loopyard — the read-only code tree (D12).
//
// ONE dist/: the repo root's. `dist:mac` runs with cwd frontend/apps/desktop, so no
// path here is cwd-relative (spec m1). smoke.mjs asserts the yml `from` prefix and
// DIST name the same dir.
//
//   node scripts/stage-origin.mjs [--dist DIR] [--arch x64|arm64]...
import { createHash } from 'node:crypto';
import { execFileSync } from 'node:child_process';
import { existsSync, mkdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

export const DIR = path.dirname(fileURLToPath(import.meta.url));
// scripts → desktop → apps → frontend → repo root
export const DIST = path.resolve(DIR, '../../../../dist');

// electron-builder `${arch}` → origin_bundle slug arch. `${arch}` can't be mapped
// inside YAML, so staged dirs are named by the electron-builder arch.
export const ARCH_MAP = Object.freeze({ x64: 'x86_64', arm64: 'arm64' });

export const slugFor = (arch) => {
  if (!ARCH_MAP[arch]) throw new Error(`unknown electron-builder arch "${arch}" (want one of ${Object.keys(ARCH_MAP).join(', ')})`);
  return `macos-${ARCH_MAP[arch]}`;
};
export const tarballName = (arch) => `loopyard-origin-${slugFor(arch)}.tar.gz`;
export const stagedDirName = (arch) => {
  slugFor(arch); // validates
  return `origin-mac-${arch}`;
};

const sha256File = (p) => createHash('sha256').update(readFileSync(p)).digest('hex');

/** Verify + unpack one arch; returns the staged dir. Throws with one actionable line. */
export function stageArch(arch, { dist = DIST } = {}) {
  const slug = slugFor(arch);
  const tgz = path.join(dist, tarballName(arch));
  if (!existsSync(tgz)) {
    throw new Error(`${tgz} missing — build it first: python -m mcp_loops.origin_bundle build --target ${slug} --out dist/ (from the repo root)`);
  }
  const shaFile = `${tgz}.sha256`;
  if (!existsSync(shaFile)) throw new Error(`${shaFile} missing — the builder writes it next to the tarball`);
  const want = (readFileSync(shaFile, 'utf8').trim().split(/\s+/)[0] || '').toLowerCase();
  const got = sha256File(tgz);
  if (!/^[0-9a-f]{64}$/.test(want)) throw new Error(`${shaFile}: first field is not a sha256 hex digest`);
  if (got !== want) throw new Error(`${tgz}: sha256 ${got} != ${want} from ${path.basename(shaFile)}`);

  const out = path.join(dist, stagedDirName(arch));
  rmSync(out, { recursive: true, force: true });
  mkdirSync(out, { recursive: true });
  execFileSync('tar', ['-xzf', tgz, '-C', out, '--strip-components=1'], { stdio: ['ignore', 'ignore', 'pipe'] });
  const yard = path.join(out, 'bin', 'yard');
  if (!existsSync(yard)) throw new Error(`${tgz} unpacked without bin/yard — not a loopyard/ bundle tarball`);
  if (existsSync(path.join(out, 'loopyard'))) throw new Error(`${out}/loopyard exists — the top dir was not stripped`);
  writeFileSync(path.join(out, 'DESKTOP_MANAGED'), 'Managed by the Loopyard desktop app. Download a new DMG to update.\n');
  return out;
}

export function stageOrigin({ dist = DIST, arches = Object.keys(ARCH_MAP), log = () => {} } = {}) {
  return arches.map((arch) => {
    const out = stageArch(arch, { dist });
    log(`staged ${tarballName(arch)} → ${out}`);
    return out;
  });
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const argv = process.argv.slice(2);
  let dist = DIST;
  const arches = [];
  for (let i = 0; i < argv.length; i += 1) {
    if (argv[i] === '--dist') dist = path.resolve(argv[++i]);
    else if (argv[i] === '--arch') arches.push(argv[++i]);
    else { console.error(`stage-origin: unknown arg ${argv[i]}`); process.exit(2); }
  }
  try {
    stageOrigin({ dist, arches: arches.length ? arches : undefined, log: (m) => console.log(`stage-origin: ${m}`) });
  } catch (e) {
    console.error(`stage-origin: ${e.message}`);
    process.exit(1);
  }
}
