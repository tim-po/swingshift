// One shared, health-aware origin picker — the FULL fleet, not just `local`.
// Consumes /api/loops/origin-picker (local first, reachable next, unreachable
// shown but DISABLED with an honest reason, sessions as "(session)" compute).
// Falls back to the raw origins list until the picker source answers.
import { useQuery } from '@tanstack/react-query';
import type { OriginPickerOption } from '@loopyard/api';
import { config } from '../../config';
import { origins, useOrigins } from '../../scope/queries';
import './origins.css';

export function useOriginPickerOptions(): OriginPickerOption[] {
  const picker = useQuery({ queryKey: ['origin-picker'], queryFn: origins.picker, refetchInterval: config.pollMs * 6 });
  const raw = useOrigins();
  const opts = picker.data?.options ?? [];
  if (opts.length) return opts;
  const fallback = (raw.data?.origins ?? []).map((o) => ({ id: o.id, name: o.name, label: o.name || o.id, kind: o.kind }));
  return fallback.length ? fallback : [{ id: 'local', label: 'local' }];
}

export interface OriginPickerProps {
  value: string;
  onChange(id: string): void;
  id?: string;
  className?: string;
  disabled?: boolean;
  'aria-label'?: string;
}

export function OriginPicker({ value, onChange, id, className, disabled, ...rest }: OriginPickerProps) {
  const opts = useOriginPickerOptions();
  const cur = opts.find((o) => o.id === value);
  return (
    <select
      id={id}
      className={'og-picker' + (className ? ' ' + className : '')}
      value={cur ? value : ''}
      disabled={disabled}
      onChange={(e) => onChange(e.target.value)}
      aria-label={rest['aria-label'] ?? 'Origin'}
      title={cur?.disabled && cur.note ? cur.note : undefined}
    >
      {!cur && <option value="" disabled>{value ? `${value} (unavailable)` : 'pick an origin'}</option>}
      {opts.map((o) => (
        <option key={o.id} value={o.id} disabled={!!o.disabled}>
          {(o.label || o.name || o.id) + (o.disabled && o.note ? ` — ${o.note}` : '')}
        </option>
      ))}
    </select>
  );
}

export default OriginPicker;
