from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

def patch_graph_py():
    path = ROOT / "src/waggle/graph.py"
    with path.open("r") as f:
        content = f.read()

    aggregate_code = """
    def aggregate(
        self,
        *,
        query: str = "",
        node_types: list[str] | None = None,
        tags: list[str] | None = None,
        max_nodes: int = 1000,
        max_depth: int = 1,
        agent_id: str = "",
        project: str = "",
        session_id: str = "",
    ) -> SubgraphResult:
        if max_nodes < 1:
            raise ValueError("max_nodes must be at least 1.")
        if max_depth < 0:
            raise ValueError("max_depth cannot be negative.")

        with self._lock, self._connect() as connection:
            node_rows = connection.execute(
                \"\"\"
                SELECT id, agent_id, project, session_id, context_window_id, label, content, node_type, tags,
                       source_prompt, metadata, evidence_records, valid_from, valid_to, created_at,
                       updated_at, access_count, embedding, tenant_id
                FROM nodes
                WHERE tenant_id = ?
                \"\"\",
                (self.tenant_id,),
            ).fetchall()

            total_nodes = len(node_rows)
            if total_nodes == 0:
                return SubgraphResult(query=query, total_nodes_in_graph=0)

            active_session_id = _retrieval_session_scope(
                agent_id=agent_id,
                project=project,
                session_id=session_id,
            )

            target_types = {t.lower() for t in node_types} if node_types else None
            target_tags = {t.lower() for t in tags} if tags else None

            candidates: list[Node] = []
            embeddings_by_id: dict[str, np.ndarray] = {}
            for row in node_rows:
                node = self._row_to_node(row)
                if not _scope_matches(node, agent_id=agent_id, project=project, session_id=active_session_id):
                    continue
                if target_types and node.node_type.value.lower() not in target_types:
                    continue
                if target_tags:
                    node_tags = {t.lower() for t in node.tags}
                    if not any(tag in node_tags for tag in target_tags):
                        continue
                candidates.append(node)
                if row["embedding"] is not None:
                    embeddings_by_id[node.id] = self.embedding_model.from_bytes(row["embedding"])

            if not candidates:
                return SubgraphResult(query=query, total_nodes_in_graph=total_nodes)

            if query.strip():
                expanded_query = self._expand_query_aliases(query)
                query_embedding = self.embedding_model.embed(expanded_query)
                
                scored_candidates = []
                for node in candidates:
                    similarity = 0.0
                    emb = embeddings_by_id.get(node.id)
                    if emb is not None:
                        similarity = max(self.embedding_model.cosine_similarity(query_embedding, emb), 0.0)
                    scored_candidates.append((similarity, node))
                
                scored_candidates.sort(key=lambda item: item[0], reverse=True)
                selected_nodes = [node for _, node in scored_candidates[:max_nodes]]
            else:
                candidates.sort(key=lambda node: node.updated_at.timestamp(), reverse=True)
                selected_nodes = candidates[:max_nodes]

            if max_depth > 0 and selected_nodes:
                selected_ids = {node.id for node in selected_nodes}
                expanded_ids = set(selected_ids)
                current_frontier = set(selected_ids)

                for _ in range(max_depth):
                    if not current_frontier:
                        break
                    next_frontier = set()
                    edges = self._fetch_edges_for_nodes(connection, list(current_frontier))
                    for edge in edges:
                        neighbor_id = edge.target_id if edge.source_id in current_frontier else edge.source_id
                        if neighbor_id not in expanded_ids:
                            expanded_ids.add(neighbor_id)
                            next_frontier.add(neighbor_id)
                    current_frontier = next_frontier

                if len(expanded_ids) > len(selected_ids):
                    missing_ids = expanded_ids - selected_ids
                    if missing_ids:
                        placeholders = ", ".join("?" for _ in missing_ids)
                        missing_rows = connection.execute(
                            f\"\"\"
                            SELECT id, agent_id, project, session_id, context_window_id, label, content, node_type, tags,
                                   source_prompt, metadata, evidence_records, valid_from, valid_to, created_at,
                                   updated_at, access_count, embedding, tenant_id
                            FROM nodes
                            WHERE tenant_id = ? AND id IN ({placeholders})
                            \"\"\",
                            (self.tenant_id, *missing_ids)
                        ).fetchall()
                        for row in missing_rows:
                            selected_nodes.append(self._row_to_node(row))

            selected_ids = [node.id for node in selected_nodes]
            edges = self._fetch_edges_for_nodes(connection, selected_ids)
            self._increment_access_counts(connection, selected_ids)
            for node in selected_nodes:
                node.access_count += 1

            return SubgraphResult(
                nodes=selected_nodes,
                edges=edges,
                retrieval_mode="aggregate",
                query=query,
                total_nodes_in_graph=total_nodes,
            )

    def tiered_query(
"""

    if "def aggregate(" not in content:
        content = content.replace("    def tiered_query(", aggregate_code)
        with path.open("w") as f:
            f.write(content)
        print("Patched graph.py")
    else:
        print("graph.py already patched")

def patch_neo4j_graph_py():
    path = ROOT / "src/waggle/neo4j_graph.py"
    with path.open("r") as f:
        content = f.read()

    aggregate_code = """
    def aggregate(
        self,
        *,
        query: str = "",
        node_types: list[str] | None = None,
        tags: list[str] | None = None,
        max_nodes: int = 1000,
        max_depth: int = 1,
        agent_id: str = "",
        project: str = "",
        session_id: str = "",
    ) -> SubgraphResult:
        query_text = query.strip()
        if max_nodes < 1:
            raise ValueError("max_nodes must be at least 1.")
        if max_depth < 0:
            raise ValueError("max_depth cannot be negative.")

        with self._lock, self._session() as session:
            node_records = [
                record["n"]
                for record in session.run(
                    "MATCH (n:MemoryNode {tenant_id: $tenant_id}) RETURN n",
                    tenant_id=self.tenant_id,
                )
            ]
            total_nodes = len(node_records)
            if total_nodes == 0:
                return SubgraphResult(query=query_text, total_nodes_in_graph=0)

            target_types = {t.lower() for t in node_types} if node_types else None
            target_tags = {t.lower() for t in tags} if tags else None

            candidates: list[Node] = []
            embeddings_by_id: dict[str, np.ndarray] = {}
            for props in node_records:
                node = self._node_from_props(props)
                if not _scope_matches(node, agent_id=agent_id, project=project, session_id=session_id):
                    continue
                if target_types and node.node_type.value.lower() not in target_types:
                    continue
                if target_tags:
                    node_tags = {t.lower() for t in node.tags}
                    if not any(tag in node_tags for tag in target_tags):
                        continue
                candidates.append(node)
                if props.get("embedding"):
                    embeddings_by_id[node.id] = np.array(props["embedding"], dtype=np.float32)

            if not candidates:
                return SubgraphResult(query=query_text, total_nodes_in_graph=total_nodes)

            if query_text:
                query_embedding = self.embedding_model.embed(query_text)
                scored_candidates = []
                for node in candidates:
                    similarity = 0.0
                    emb = embeddings_by_id.get(node.id)
                    if emb is not None:
                        similarity = max(self.embedding_model.cosine_similarity(query_embedding, emb), 0.0)
                    scored_candidates.append((similarity, node))
                scored_candidates.sort(key=lambda item: item[0], reverse=True)
                selected_nodes = [node for _, node in scored_candidates[:max_nodes]]
            else:
                candidates.sort(key=lambda node: node.updated_at.timestamp(), reverse=True)
                selected_nodes = candidates[:max_nodes]

            if max_depth > 0 and selected_nodes:
                selected_ids = [node.id for node in selected_nodes]
                graph = self._load_graph(session)
                expanded_depths = self._expand_node_depths(graph, selected_ids, max_depth)
                expanded_ids = set(expanded_depths.keys())
                missing_ids = expanded_ids - {node.id for node in selected_nodes}
                if missing_ids:
                    for props in node_records:
                        if props["id"] in missing_ids:
                            selected_nodes.append(self._node_from_props(props))

            selected_ids = [node.id for node in selected_nodes]
            edges = self._fetch_edges_for_nodes(session, selected_ids)
            self._increment_access_counts(session, selected_ids)
            for node in selected_nodes:
                node.access_count += 1

            return SubgraphResult(
                nodes=selected_nodes,
                edges=edges,
                retrieval_mode="aggregate",
                query=query_text,
                total_nodes_in_graph=total_nodes,
            )

    def query(
"""
    if "def aggregate(" not in content:
        content = content.replace("    def query(", aggregate_code)
        with path.open("w") as f:
            f.write(content)
        print("Patched neo4j_graph.py")
    else:
        print("neo4j_graph.py already patched")


def patch_server_py():
    path = ROOT / "src/waggle/server.py"
    with path.open("r") as f:
        content = f.read()

    tool_def = """
            types.Tool(
                name="aggregate_graph",
                description=(
                    "Retrieve a broad set of nodes bypassing standard semantic limits, optimized for "
                    "global aggregation and map-reduce tasks. Supports filtering by node_type and tags."
                ),
                inputSchema=_object_input_schema(
                    {
                        "query": {"type": "string", "description": "Optional natural-language search query to rank the broad retrieval."},
                        "node_types": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Optional list of node types to filter by (e.g., 'fact', 'entity')."
                        },
                        "tags": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Optional list of tags to require."
                        },
                        "max_nodes": {"type": "integer", "description": "Maximum number of nodes to return (default 100, up to 1000)."},
                        "max_depth": {"type": "integer", "description": "Relationship traversal depth around matching nodes."},
                        **_scope_properties(),
                    },
                ),
            ),
            types.Tool(
                name="query_graph","""

    if "name=\"aggregate_graph\"" not in content:
        content = content.replace("            types.Tool(\n                name=\"query_graph\",", tool_def)

    tool_handler = """
                elif name == "aggregate_graph":
                    subgraph = graph.aggregate(
                        query=arguments.get("query", ""),
                        node_types=arguments.get("node_types"),
                        tags=arguments.get("tags"),
                        max_nodes=int(arguments.get("max_nodes", 100)),
                        max_depth=int(arguments.get("max_depth", 1)),
                        agent_id=arguments.get("agent_id", ""),
                        project=arguments.get("project", ""),
                        session_id=arguments.get("session_id", ""),
                    )
                    result = self._tool_result(
                        serialize_subgraph(subgraph),
                        self._subgraph_payload(subgraph),
                    )
                elif name == "query_graph":"""

    if "elif name == \"aggregate_graph\":" not in content:
        content = content.replace("                elif name == \"query_graph\":", tool_handler)

    tool_asserts = """
        if name == "aggregate_graph":
            self._assert_payload_size(arguments.get("query", ""), limit, "aggregate_graph.query")
            self._assert_payload_size(arguments.get("agent_id", ""), limit, "aggregate_graph.agent_id")
            self._assert_payload_size(arguments.get("project", ""), limit, "aggregate_graph.project")
            self._assert_payload_size(arguments.get("session_id", ""), limit, "aggregate_graph.session_id")
        if name == "query_graph":"""
    if "name == \"aggregate_graph\"" not in content:
        content = content.replace("        if name == \"query_graph\":", tool_asserts)

    with path.open("w") as f:
        f.write(content)
    print("Patched server.py")

if __name__ == "__main__":
    patch_graph_py()
    patch_neo4j_graph_py()
    patch_server_py()
