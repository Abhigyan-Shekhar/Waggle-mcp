import { useEffect } from "react";
import { AnimatePresence, motion } from "framer-motion";

export function ContextMenu({ menu, onClose, onAction }) {
  useEffect(() => {
    if (!menu) {
      return undefined;
    }
    const close = () => onClose();
    window.addEventListener("click", close);
    window.addEventListener("contextmenu", close);
    return () => {
      window.removeEventListener("click", close);
      window.removeEventListener("contextmenu", close);
    };
  }, [menu, onClose]);

  return (
    <AnimatePresence>
      {menu ? (
        <motion.div
          initial={{ opacity: 0, scale: 0.96 }}
          animate={{ opacity: 1, scale: 1 }}
          exit={{ opacity: 0, scale: 0.96 }}
          className="fixed z-50 min-w-44 rounded-xl border border-white/10 bg-[#171a1f] p-2 shadow-2xl"
          style={{ left: menu.x, top: menu.y }}
        >
          {menu.actions.map((action) => (
            <button
              key={action.id}
              className="block w-full rounded-lg px-3 py-2 text-left text-sm text-white transition hover:bg-white/8"
              onClick={() => onAction(action.id, menu.nodeId)}
              type="button"
            >
              {action.label}
            </button>
          ))}
        </motion.div>
      ) : null}
    </AnimatePresence>
  );
}
