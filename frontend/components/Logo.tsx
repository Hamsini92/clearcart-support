export default function Logo({ size = 28 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 32 32" fill="none" aria-hidden="true">
      <defs>
        <linearGradient id="logoGradient" x1="0" y1="0" x2="32" y2="32">
          <stop offset="0" stopColor="#9d95ff" />
          <stop offset="1" stopColor="#5346e0" />
        </linearGradient>
      </defs>
      <rect width="32" height="32" rx="9" fill="url(#logoGradient)" />
      <circle
        cx="16"
        cy="16"
        r="7"
        stroke="white"
        strokeWidth="2.6"
        fill="none"
        strokeLinecap="round"
        strokeDasharray="34 10"
        strokeDashoffset="-6"
        transform="rotate(90 16 16)"
      />
    </svg>
  );
}
