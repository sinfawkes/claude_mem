"""
Knowledge graph for tracking relationships between code entities and memories.

Node types: file, function, pattern, decision
Edge types: contains, applies_to, led_to, uses, superseded_by
"""

from __future__ import annotations

import logging
import os
import pickle
from typing import Optional

import networkx as nx

logger = logging.getLogger(__name__)

_NODE_TYPES = {"file", "function", "pattern", "decision", "memory"}
_EDGE_TYPES = {"contains", "applies_to", "led_to", "uses", "superseded_by"}


class KnowledgeGraph:
    def __init__(self, graph_path: str):
        self._path = os.path.expanduser(graph_path)
        self._graph: nx.DiGraph = self._load()

    def _load(self) -> nx.DiGraph:
        if os.path.exists(self._path):
            try:
                with open(self._path, "rb") as f:
                    return pickle.load(f)
            except Exception:
                logger.warning("Could not load knowledge graph from %s; starting fresh", self._path)
        return nx.DiGraph()

    def save(self) -> None:
        os.makedirs(os.path.dirname(self._path), exist_ok=True)
        with open(self._path, "wb") as f:
            pickle.dump(self._graph, f)

    def add_node(self, node_id: str, node_type: str, **attrs) -> None:
        if node_type not in _NODE_TYPES:
            raise ValueError("Unknown node type: {}. Must be one of {}".format(node_type, _NODE_TYPES))
        self._graph.add_node(node_id, type=node_type, **attrs)

    def add_edge(self, from_id: str, to_id: str, relation: str, **attrs) -> None:
        if relation not in _EDGE_TYPES:
            raise ValueError("Unknown edge relation: {}. Must be one of {}".format(relation, _EDGE_TYPES))
        self._graph.add_edge(from_id, to_id, relation=relation, **attrs)

    def get_neighbors(self, node_id: str, max_hops: int = 2) -> dict:
        """Return all nodes reachable from node_id within max_hops."""
        if node_id not in self._graph:
            return {"nodes": [], "edges": []}

        reachable_nodes = set()
        frontier = {node_id}
        for _ in range(max_hops):
            next_frontier = set()
            for n in frontier:
                next_frontier.update(self._graph.successors(n))
                next_frontier.update(self._graph.predecessors(n))
            reachable_nodes.update(frontier)
            frontier = next_frontier - reachable_nodes

        subgraph = self._graph.subgraph(reachable_nodes)
        return self._serialize_graph(subgraph)

    def to_dict(self) -> dict:
        return self._serialize_graph(self._graph)

    def _serialize_graph(self, g: nx.DiGraph) -> dict:
        nodes = [{"id": n, **data} for n, data in g.nodes(data=True)]
        edges = [
            {"from": u, "to": v, **data}
            for u, v, data in g.edges(data=True)
        ]
        return {"nodes": nodes, "edges": edges}

    def link_memory_to_files(self, memory_id: str, files: list[str]) -> None:
        """Add a memory node and link it to file nodes."""
        self.add_node(memory_id, "memory")
        for file_path in files:
            if file_path not in self._graph:
                self.add_node(file_path, "file", path=file_path)
            self.add_edge(memory_id, file_path, "applies_to")

    def mark_superseded(self, old_memory_id: str, new_memory_id: str) -> None:
        """Link old memory to its replacement."""
        self.add_node(new_memory_id, "memory")
        self.add_edge(old_memory_id, new_memory_id, "superseded_by")
