# Export & Load Graph as Cypher

`export_cypher` exports the Waggle memory graph as a Neo4j-compatible `.cypher` script. You can load the output into a local Neo4j instance to run arbitrary Cypher queries.

## Tool

**MCP tool:** `export_cypher`  
**HTTP endpoint:** `GET /api/graph/export?format=cypher`

### Parameters

| Parameter | Type | Default | Description |
|---|---|---|---|
| `output_path` | `string` | Auto-generated | Path to write the `.cypher` file |
| `project` | `string` | `""` | Scope export to a project |
| `agent_id` | `string` | `""` | Scope export to an agent |
| `session_id` | `string` | `""` | Scope export to a session |

### Returns

```json
{
  "output_path": "/path/to/waggle-memory-20250101-120000.cypher",
  "tenant_id": "...",
  "node_count": 42,
  "edge_count": 17,
  "format": "cypher"
}
```

## Output Shape

The generated `.cypher` script contains three sections:

### 1. Uniqueness Constraint

```cypher
CREATE CONSTRAINT waggle_memory_id IF NOT EXISTS FOR (n:Memory) REQUIRE n.id IS UNIQUE;
```

This ensures every `:Memory` node has a unique `id` so edges can reference nodes by ID without duplicates.

### 2. Node Creation (`CREATE`)

One `CREATE` per node, labelled `:Memory:{NodeType}` (e.g. `:Memory:fact`, `:Memory:entity`, `:Memory:decision`). Properties include `id`, `label`, `content`, `node_type`, `project`, `agent_id`, `session_id`, `created_at`, and optionally `community_id`/`community_label`.

```cypher
CREATE (:Memory:fact {id: 'abc123', label: 'Project deadline', content: 'Due next Friday', node_type: 'fact', project: 'my-project', agent_id: '', session_id: '', created_at: '2025-01-01T12:00:00'});
```

### 3. Edge Creation (`MATCH ... CREATE`)

One `MATCH` + `CREATE` per edge. Nodes are matched by `id`, then an edge is created with the edge's `weight` value.

```cypher
MATCH (a:Memory {id: 'abc123'}), (b:Memory {id: 'def456'}) CREATE (a)-[:RELATES_TO {weight: 1.0, confidence: 1.0}]->(b);
```

Relationship types are sanitized to uppercase snake_case (e.g. `relates_to` → `RELATES_TO`, `derived_from` → `DERIVED_FROM`). String values are escaped to prevent Cypher injection.

## Loading into Neo4j

### Prerequisites

- [Neo4j](https://neo4j.com/download/) (local or remote) running with your preferred authentication
- `cypher-shell` (included with Neo4j) or access to Neo4j Browser

### Option A: Using `cypher-shell`

```bash
# Load the generated script
cat waggle-memory-20250101-120000.cypher | cypher-shell -u neo4j -p your-password
```

### Option B: Using Neo4j Browser

1. Open Neo4j Browser at `http://localhost:7474`
2. Connect to your database
3. Click the `:source` button in the editor toolbar (or press `Cmd+O` / `Ctrl+O`)
4. Select the generated `.cypher` file
5. The script executes, creating all nodes and edges

### Option C: Using `cypher-shell` with file input (large datasets)

```bash
cypher-shell -u neo4j -p your-password -f waggle-memory-20250101-120000.cypher
```

### Verifying the Import

```cypher
// Count imported nodes
MATCH (n:Memory) RETURN count(n) AS node_count;

// Count imported edges
MATCH ()-[r]->() RETURN count(r) AS edge_count;

// Sample the graph
MATCH (n:Memory)-[r]->(m:Memory) RETURN n.label, type(r), m.label LIMIT 20;
```

## Troubleshooting

| Issue | Solution |
|---|---|
| `Neo4jError: Profiling is not supported` | Use `cypher-shell` without `--profile`; the script uses `CREATE` not `PROFILE` |
| `Neo4jError: Already exists` | The `CREATE CONSTRAINT IF NOT EXISTS` prevents duplicate constraint errors |
| Connection refused | Ensure Neo4j is running and `cypher-shell` points to the correct `bolt://` URI |
| Node not found during edge creation | The export script is intentionally ordered — all node `CREATE` statements appear before `MATCH ... CREATE` edge statements, so referenced nodes exist first |
