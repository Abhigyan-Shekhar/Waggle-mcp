import os
import sqlite3


def print_memory_stats(db_path: str = None, model: str = None) -> None:
    """Print memory usage statistics for the Waggle database."""
    if db_path is None:
        db_path = os.getenv("WAGGLE_DB_PATH", "~/.waggle/waggle.db")
    db_path = os.path.expanduser(db_path)

    print("Memory Statistics")
    print("-----------------")

    nodes = 0
    edges = 0
    conversations = 0
    db_size_bytes = 0
    backend = "SQLite"
    embedding_model = model or os.getenv("WAGGLE_MODEL", "unknown")

    if os.path.exists(db_path):
        db_size_bytes = os.path.getsize(db_path)
        try:
            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()
            try:
                cursor.execute("SELECT COUNT(*) FROM nodes")
                nodes = cursor.fetchone()[0]
            except sqlite3.OperationalError:
                pass
            try:
                cursor.execute("SELECT COUNT(*) FROM edges")
                edges = cursor.fetchone()[0]
            except sqlite3.OperationalError:
                pass
            try:
                cursor.execute("SELECT COUNT(*) FROM conversations")
                conversations = cursor.fetchone()[0]
            except sqlite3.OperationalError:
                pass
            conn.close()
        except Exception:
            pass

    db_size_mb = db_size_bytes / (1024 * 1024)

    print(f"Nodes: {nodes}")
    print(f"Edges: {edges}")
    print(f"Conversations: {conversations}")
    print(f"Database Size: {db_size_mb:.1f} MB")
    print(f"Backend: {backend}")
    print(f"Embedding Model: {embedding_model}")
