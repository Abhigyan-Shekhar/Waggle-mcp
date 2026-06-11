import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# Ensure src is in the python path
sys.path.insert(0, str(ROOT / "src"))

from waggle.graph import MemoryGraph


def main():
    print("Testing MemoryGraph.aggregate()...")

    # Initialize the graph pointing to the local default db
    db_path = "/Users/abhigyanshekhar/.waggle/memory.db"

    try:
        graph = MemoryGraph(db_path=db_path)

        # Call the new aggregate method
        result = graph.aggregate(
            query="",
            max_nodes=100,
            max_depth=1
        )

        print(f"Success! Aggregate retrieved {len(result.nodes)} nodes.")
        if result.nodes:
            print(f"Top node label: {result.nodes[0].label}")
            print(f"Top node type: {result.nodes[0].node_type.value}")

    except Exception as e:
        print(f"Error testing aggregate: {e}")

if __name__ == "__main__":
    main()
