// The two flip-dot displays in the app. At most one per screen: the shift board
// on Home, the sign in the top bar everywhere else.
import { Link } from 'react-router-dom';
import type { OverviewModel } from '@loopyard/api';
import { DotBoard } from './DotBoard';
import { SB, SIGN_COLS, shiftBoardRows, shiftItems, shiftLabel, shiftRows, signText } from './shift';

export { DotBoard } from './DotBoard';

/** Home: a departures board of the loops on shift — what needs you first, then what's running. */
export function ShiftBoard({ m }: { m: Pick<OverviewModel, 'attention' | 'running'> }) {
  const rows = shiftRows(m);
  if (!rows.length) return null;
  return (
    <DotBoard
      className="ov-board"
      cols={SB.cols}
      rows={shiftBoardRows(rows.length)}
      items={shiftItems(rows)}
      label={shiftLabel(rows)}
      min={1.5}
      max={4}
      head={
        <>
          <span style={{ ['--col' as string]: SB.nameX }}>Loop</span>
          <span style={{ ['--col' as string]: SB.turnX }}>Turn</span>
          <span style={{ ['--col' as string]: SB.statX }}>Status</span>
        </>
      }
    />
  );
}

/** Top bar: "2 NEED YOU" on red discs, else "3 RUNNING"; nothing when the yard is quiet. Links Home. */
export function ShiftSign({ m }: { m: Pick<OverviewModel, 'attention' | 'running'> }) {
  const s = signText(m);
  if (!s) return null;
  return (
    <Link to="/overview" className="fd-signlink" title="Open Home" aria-live="polite">
      <DotBoard className="sm" cols={SIGN_COLS} rows={9} pitch={2.5} alert={s.alert} label={s.text.toLowerCase()}
        items={[{ text: s.text, x: 0, y: 1, align: 'center', w: SIGN_COLS }]} />
    </Link>
  );
}
