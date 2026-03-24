from __future__ import annotations

import networkx as nx
import plotly.graph_objects as go

from benchmark.attribution.knowledge import KnowledgeGraphStore


class KnowledgeGraphRenderer:
    """Render a knowledge graph as an interactive plotly figure."""

    def __init__(self) -> None:
        pass

    def render(self, store: KnowledgeGraphStore) -> go.Figure:
        """Build a layered plotly figure from a knowledge graph store."""
        commits, entities, files = self._classify(store)
        pos = self._layout(store, commits, entities, files)
        edges = self._edges(store, pos)
        width, height, xaxis, yaxis = self._frame(pos)

        fig = go.Figure()
        for trace in self._traces(edges):
            fig.add_trace(trace)
        for trace in self._markers(commits, entities, files, pos):
            fig.add_trace(trace)

        fig.update_layout(
            width=width,
            height=height,
            showlegend=True,
            plot_bgcolor="white",
            paper_bgcolor="white",
            margin={"l": 20, "r": 60, "t": 20, "b": 20},
            dragmode="pan",
            xaxis=xaxis,
            yaxis=yaxis,
        )
        return fig

    def _classify(
        self,
        store: KnowledgeGraphStore,
    ) -> tuple[
        list[tuple[str, dict]],
        list[tuple[str, dict]],
        list[tuple[str, dict]],
    ]:
        commits: list[tuple[str, dict]] = []
        files: list[tuple[str, dict]] = []
        entities: list[tuple[str, dict]] = []

        for node, data in store.graph.nodes(data=True):
            if data["kind"] == "commit":
                commits.append((node, data))
            elif data["kind"] == "file":
                files.append((node, data))
            elif data["kind"] in {"class", "function", "method", "program"}:
                entities.append((node, data))

        members = [node for node, _ in commits]

        dag = nx.DiGraph()
        dag.add_nodes_from(members)

        for source, target, data in store.graph.edges(data=True):
            if (
                data["edge"] == "PRECEDES"
                and source in members
                and target in members
            ):
                dag.add_edge(source, target)

        order = list(nx.topological_sort(dag))
        commits = [(node, store.graph.nodes[node]) for node in order]

        return commits, entities, files

    def _layout(
        self,
        store: KnowledgeGraphStore,
        commits: list[tuple[str, dict]],
        entities: list[tuple[str, dict]],
        files: list[tuple[str, dict]],
    ) -> dict[str, tuple[float, float]]:
        gap = 1.0
        pos: dict[str, tuple[float, float]] = {}

        for index, (node, _) in enumerate(commits):
            pos[node] = (index * gap, 0.0)

        anchors: list[tuple[str, float]] = []
        for node, _ in entities:
            xs = [
                pos[source][0]
                for source, _, data in store.graph.in_edges(node, data=True)
                if data["edge"] in {"ADD", "EDIT", "DELETE"} and source in pos
            ]
            center = sum(xs) / len(xs) if xs else 0.0
            anchors.append((node, center))

        anchors.sort(key=lambda item: item[1])

        placed: list[tuple[str, float]] = []
        for node, center in anchors:
            if not placed:
                x = center
            else:
                x = max(center, placed[-1][1] + 0.35)
            placed.append((node, x))
            pos[node] = (x, 1.0)

        anchors = []
        for node, _ in files:
            xs = [
                pos[source][0]
                for source, _, data in store.graph.in_edges(node, data=True)
                if data["edge"] == "IN" and source in pos
            ]
            center = sum(xs) / len(xs) if xs else 0.0
            anchors.append((node, center))

        anchors.sort(key=lambda item: item[1])

        placed = []
        for node, center in anchors:
            if not placed:
                x = center
            else:
                x = max(center, placed[-1][1] + gap)
            placed.append((node, x))
            pos[node] = (x, 2.0)

        return pos

    def _edges(
        self,
        store: KnowledgeGraphStore,
        pos: dict[str, tuple[float, float]],
    ) -> dict[str, tuple[list, list]]:
        result: dict[str, tuple[list, list]] = {
            "ADD": ([], []),
            "EDIT": ([], []),
            "DELETE": ([], []),
            "MOVE": ([], []),
            "IN": ([], []),
            "PRECEDES": ([], []),
        }

        for source, target, data in store.graph.edges(data=True):
            if source not in pos or target not in pos:
                continue

            x0, y0 = pos[source]
            x1, y1 = pos[target]
            label = data["edge"]

            if (
                store.graph.nodes[source]["kind"] == "commit"
                and store.graph.nodes[target]["kind"]
                in {"class", "function", "method", "program"}
                and label in {"ADD", "EDIT", "DELETE", "MOVE"}
            ):
                xs, ys = result[label]
                xs.extend([x0, x1, None])
                ys.extend([y0, y1, None])

            if label == "IN":
                xs, ys = result["IN"]
                xs.extend([x0, x1, None])
                ys.extend([y0, y1, None])

            if label == "PRECEDES":
                xs, ys = result["PRECEDES"]
                xs.extend([x0, x1, None])
                ys.extend([y0, y1, None])

            if label == "MOVE":
                xs, ys = result["MOVE"]
                xs.extend([x0, x1, None])
                ys.extend([y0, y1, None])

        return result

    def _traces(
        self,
        edges: dict[str, tuple[list, list]],
    ) -> list[go.Scatter]:
        style = {
            "ADD": {"width": 1.6, "color": "#2f855a"},
            "EDIT": {"width": 1.6, "color": "#3182ce"},
            "DELETE": {"width": 1.6, "color": "#c53030"},
            "MOVE": {"width": 1.6, "color": "#d69e2e"},
            "IN": {"width": 1.0, "color": "#bdbdbd"},
            "PRECEDES": {"width": 1.0, "color": "grey"},
        }

        traces = []
        for label, (xs, ys) in edges.items():
            traces.append(
                go.Scatter(
                    x=xs,
                    y=ys,
                    mode="lines",
                    line=style[label],
                    hoverinfo="none",
                    name=label,
                )
            )
        return traces

    def _markers(
        self,
        commits: list[tuple[str, dict]],
        entities: list[tuple[str, dict]],
        files: list[tuple[str, dict]],
        pos: dict[str, tuple[float, float]],
    ) -> list[go.Scatter]:
        traces = []

        xs = []
        ys = []
        hover = []
        scores = []

        for node, data in commits:
            payload = data["payload"]
            x, y = pos[node]
            xs.append(x)
            ys.append(y)
            scores.append(payload["score"])
            hover.append(
                f"<b>{payload['type']}</b><br>"
                f"sha: {payload['sha']}<br>"
                f"score: {payload['score']:.2f}<br>"
                f"{payload['message'][:180]}"
            )

        traces.append(
            go.Scatter(
                x=xs,
                y=ys,
                mode="markers",
                hovertext=hover,
                hoverinfo="text",
                marker={
                    "size": 10,
                    "color": scores,
                    "cmin": 0.0,
                    "cmax": 1.0,
                    "colorscale": [
                        [0.00, "#e2e8f0"],
                        [0.30, "#a9d6f5"],
                        [0.60, "#5dade2"],
                        [1.00, "#2b6cb0"],
                    ],
                    "symbol": "circle",
                    "line": {"width": 1.0, "color": "#2d3748"},
                },
                name="commit",
            )
        )

        xs = []
        ys = []
        hover = []
        colors = []

        for node, data in entities:
            if node not in pos:
                continue

            payload = data["payload"]
            x, y = pos[node]
            xs.append(x)
            ys.append(y)
            hover.append(
                f"<b>{data['kind']}</b><br>"
                f"name: {payload['name']}<br>"
                f"path: {payload['path']}<br>"
                f"urn: {payload['urn']}<br>"
            )

            if data["kind"] == "class":
                colors.append("#ef8a62")
            elif data["kind"] == "method":
                colors.append("#67a9cf")
            elif data["kind"] == "program":
                colors.append("#b2abd2")
            else:
                colors.append("#7bccc4")

        traces.append(
            go.Scatter(
                x=xs,
                y=ys,
                mode="markers",
                hovertext=hover,
                hoverinfo="text",
                marker={
                    "size": 10,
                    "color": colors,
                    "symbol": "triangle-up",
                    "line": {"width": 1.0, "color": "#2d3748"},
                },
                name="entity",
            )
        )

        xs = []
        ys = []
        hover = []

        for node, data in files:
            x, y = pos[node]
            xs.append(x)
            ys.append(y)
            hover.append(data["payload"]["path"])

        traces.append(
            go.Scatter(
                x=xs,
                y=ys,
                mode="markers",
                hovertext=hover,
                hoverinfo="text",
                marker={
                    "size": 10,
                    "color": "#2b6cb0",
                    "symbol": "square",
                    "line": {"width": 1.1, "color": "#2d3748"},
                },
                name="file",
            )
        )

        return traces

    def _frame(
        self,
        pos: dict[str, tuple[float, float]],
    ) -> tuple[int, int, dict, dict]:
        xs = [x for x, _ in pos.values()] or [0.0]
        ys = [y for _, y in pos.values()] or [0.0]

        lo = (min(xs), min(ys))
        hi = (max(xs), max(ys))

        pad = (0.6, 0.45)

        width = max(500, int((hi[0] - lo[0] + 2 * pad[0]) * 20) + 120)
        height = max(360, int((hi[1] - lo[1] + 2 * pad[1]) * 180) + 80)

        xaxis = {
            "showgrid": False,
            "zeroline": False,
            "visible": False,
            "range": [lo[0] - pad[0], hi[0] + pad[0]],
        }
        yaxis = {
            "showgrid": False,
            "zeroline": False,
            "visible": False,
            "range": [lo[1] - pad[1], hi[1] + pad[1]],
        }

        return width, height, xaxis, yaxis
