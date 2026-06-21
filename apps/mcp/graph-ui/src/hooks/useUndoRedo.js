import { useEffect } from "react";
import { apiRequest } from "../lib/api";
import { buildRestorePayload } from "../lib/graph-utils";

export function useUndoRedo({ graph, scope, boot, historyPast, historyFuture, setHistoryPast, setHistoryFuture, setToast, loadSnapshot }) {
  const pushHistory = async () => {
    const restorePayload = buildRestorePayload(graph, scope);
    setHistoryPast((current) => [...current, restorePayload].slice(-50));
    setHistoryFuture([]);
  };

  const restoreSnapshot = async (payload) => {
    await apiRequest("/api/graph/restore", {
      method: "POST",
      body: JSON.stringify(payload)
    });
    await loadSnapshot(scope);
  };

  const undo = async () => {
    const previous = historyPast[historyPast.length - 1];
    if (!previous || boot.sampleMode) {
      return;
    }
    const current = buildRestorePayload(graph, scope);
    setHistoryPast((items) => items.slice(0, -1));
    setHistoryFuture((items) => [...items, current]);
    await restoreSnapshot(previous);
    setToast("Undid last graph edit.");
  };

  const redo = async () => {
    const next = historyFuture[historyFuture.length - 1];
    if (!next || boot.sampleMode) {
      return;
    }
    const current = buildRestorePayload(graph, scope);
    setHistoryFuture((items) => items.slice(0, -1));
    setHistoryPast((items) => [...items, current].slice(-50));
    await restoreSnapshot(next);
    setToast("Redid graph edit.");
  };

  useEffect(() => {
    const onKeyDown = (event) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "z") {
        event.preventDefault();
        if (event.shiftKey) {
          redo().catch((error) => setToast(error.message));
        } else {
          undo().catch((error) => setToast(error.message));
        }
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [historyPast, historyFuture, graph, scope]);

  return { pushHistory, undo, redo, historyPast, historyFuture };
}
