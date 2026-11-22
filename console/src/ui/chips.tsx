// Filter chips (slice 21.3a on Workloads; shared since 21.6d): one pressed at a time.
export function Chips<T extends string>({
  label,
  value,
  onChange,
  options,
}: {
  /** what the group filters, for screen readers */
  label: string;
  value: string;
  onChange: (v: T) => void;
  options: readonly (readonly [T, string])[];
}) {
  return (
    <div className="flex flex-wrap gap-1" role="group" aria-label={label}>
      {options.map(([v, text]) => (
        <button
          key={text}
          type="button"
          aria-pressed={value === v}
          onClick={() => onChange(v)}
          className="cursor-pointer rounded-full border border-line bg-panel px-3 py-1 text-xs text-ink aria-pressed:border-accent aria-pressed:text-accent"
        >
          {text}
        </button>
      ))}
    </div>
  );
}
