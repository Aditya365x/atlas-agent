/** The Atlas mark: a stroked "A" on a monochrome tile. */
export default function Logo({ size = 26, inverted = false }: { size?: number; inverted?: boolean }) {
  const tile = inverted ? "#000" : "#fff";
  const ink = inverted ? "#fff" : "#000";
  return (
    <svg width={size} height={size} viewBox="0 0 28 28" aria-hidden className="mark">
      <rect width="28" height="28" rx="8" fill={tile} />
      <path d="M7 21 L14 6 L21 21" fill="none" stroke={ink} strokeWidth="2.4" strokeLinejoin="round" strokeLinecap="round" />
      <path d="M10 15.5 H18" stroke={ink} strokeWidth="2.4" strokeLinecap="round" />
    </svg>
  );
}
