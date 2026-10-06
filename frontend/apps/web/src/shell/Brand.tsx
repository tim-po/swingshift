import { Link } from 'react-router-dom';

/** The time-card mark: a ruled card in ink with one punched row in red — one shift, recorded. */
export function Brand() {
  return (
    <Link to="/overview" className="brand" aria-label="Swingshift home">
      <svg className="glyph" viewBox="0 0 64 64" aria-hidden="true">
        <rect x="18.5" y="4.5" width="27" height="55" fill="none" stroke="currentColor" strokeWidth="5" />
        <path d="M24 15 H40 M24 22 H40 M24 29 H40 M24 50 H40" stroke="currentColor" strokeWidth="2.5" />
        <rect x="16" y="35" width="32" height="9" fill="var(--mark)" />
      </svg>
      <span className="wm">Swingshift</span>
    </Link>
  );
}
