export function Pill({ active, children, color, onClick }) {
  return (
    <button
      onClick={onClick}
      className={`rounded-full border px-3 py-1 text-xs transition ${
        active ? "border-white/20 bg-white/12 text-white" : "border-white/10 bg-black/15 text-graph-muted hover:bg-white/8"
      }`}
      style={active && color ? { boxShadow: `0 0 0 1px ${color} inset`, color } : undefined}
      type="button"
    >
      {children}
    </button>
  );
}
