import { useEffect, useState } from "react";
import { AnimatePresence, motion } from "framer-motion";

const RELATION_TYPES = ["relates_to", "contradicts", "depends_on", "part_of", "updates", "derived_from", "similar_to"];

export function EdgeDialog({ edge, onCancel, onSave }) {
  const [relationship, setRelationship] = useState(edge?.relationship || "relates_to");

  useEffect(() => {
    setRelationship(edge?.relationship || "relates_to");
  }, [edge]);

  return (
    <AnimatePresence>
      {edge ? (
        <motion.div initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }} className="fixed inset-0 z-40 flex items-center justify-center bg-black/60 p-4">
          <motion.div initial={{ y: 12, opacity: 0 }} animate={{ y: 0, opacity: 1 }} exit={{ y: 12, opacity: 0 }} className="w-full max-w-sm rounded-2xl border border-white/10 bg-graph-panel p-5 shadow-2xl">
            <h3 className="text-lg font-semibold">Edit edge label</h3>
            <p className="mt-1 text-sm text-graph-muted">This updates the stored relationship type for the edge.</p>
            <select className="mt-4 w-full rounded-xl border border-white/10 bg-black/20 px-3 py-2 text-sm" value={relationship} onChange={(event) => setRelationship(event.target.value)}>
              {RELATION_TYPES.map((type) => (
                <option key={type} value={type}>
                  {type}
                </option>
              ))}
            </select>
            <div className="mt-4 flex justify-end gap-2">
              <button className="rounded-xl border border-white/10 px-3 py-2 text-sm" onClick={onCancel} type="button">
                Cancel
              </button>
              <button className="rounded-xl bg-white px-3 py-2 text-sm font-medium text-black" onClick={() => onSave(relationship)} type="button">
                Save
              </button>
            </div>
          </motion.div>
        </motion.div>
      ) : null}
    </AnimatePresence>
  );
}
