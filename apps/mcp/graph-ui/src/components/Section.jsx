export function Section({ title, children, extra }) {
  return (
    <section className="rounded-2xl border border-white/8 bg-white/[0.04] p-4 panel-shell">
      <div className="mb-3 flex items-center justify-between gap-3">
        <h2 className="text-xs font-semibold uppercase tracking-[0.18em] text-graph-muted">{title}</h2>
        {extra}
      </div>
      {children}
    </section>
  );
}
