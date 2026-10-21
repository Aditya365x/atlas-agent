/** A route between two waypoints: the Atlas mark. */
export default function Logo({ size = 26 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 28 28" aria-hidden>
      <rect width="28" height="28" rx="7" fill="var(--ink)" />
      <path d="M8 20 C 8 13, 20 15, 20 8" fill="none" stroke="var(--paper-2)" strokeWidth="2" strokeDasharray="2.5 2.5" strokeLinecap="round" />
      <circle cx="8" cy="20" r="2.6" fill="var(--paper-2)" />
      <circle cx="20" cy="8" r="2.6" fill="var(--ocean)" stroke="var(--paper-2)" strokeWidth="1.4" />
    </svg>
  );
}
