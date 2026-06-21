export function FileInputButton({ label, accept, onChange, disabled }) {
  return (
    <label
      className={`rounded-xl border border-white/10 px-3 py-2 text-sm text-white ${
        disabled ? "opacity-50 cursor-not-allowed" : "cursor-pointer"
      }`}
    >
      {label}
      <input className="hidden" type="file" accept={accept} onChange={onChange} disabled={disabled} />
    </label>
  );
}
