import { Modal } from './Modal';

const ROWS: [string[], string][] = [
  [['g', 'o'], 'Home'],
  [['g', 'l'], 'Loops'],
  [['g', 'p'], 'Plans'],
  [['g', 'i'], 'Issues'],
  [['g', 'm'], 'Machines'],
  [['g', 'a'], 'Roles'],
  [['g', 'j'], 'Projects'],
  [['n'], 'New loop'],
  [['/'], 'Search this page'],
  [['r'], 'Refresh now'],
  [['?'], 'This help'],
  [['Esc'], 'Close a dialog'],
];

export function ShortcutsHelp({ onClose }: { onClose(): void }) {
  return (
    <Modal label="Keyboard shortcuts" onClose={onClose}>
      <h2 className="sh-mh">Keyboard shortcuts</h2>
      <p className="sh-mhint">Work anywhere except while you're typing in a field.</p>
      <table className="sh-keys">
        <tbody>
          {ROWS.map(([keys, what]) => (
            <tr key={keys.join('')}>
              <td>
                {keys.map((k, i) => (
                  <span key={i}>
                    {i > 0 && <span className="sh-then"> then </span>}
                    <kbd>{k}</kbd>
                  </span>
                ))}
              </td>
              <td>{what}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </Modal>
  );
}
