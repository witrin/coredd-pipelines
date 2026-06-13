from __future__ import annotations

import html
import textwrap

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
        components = self._components(store, files)
        pos = self._layout(store, commits, entities, files, components)
        edges = self._edges(store, pos, files, components)
        width, height, xaxis, yaxis = self._frame(pos, edges)

        fig = go.Figure()
        for trace in self._traces(edges):
            fig.add_trace(trace)
        for trace in self._markers(
            commits, entities, files, pos, store, components
        ):
            fig.add_trace(trace)
        for trace in self._legend():
            fig.add_trace(trace)

        fig.update_layout(
            width=width,
            height=height,
            showlegend=True,
            plot_bgcolor="white",
            paper_bgcolor="white",
            margin={"l": 20, "r": 14, "t": 20, "b": 40},
            dragmode="pan",
            xaxis=xaxis,
            yaxis=yaxis,
            legend2={
                "itemclick": False,
                "itemdoubleclick": False,
                "orientation": "h",
                "x": 0.0,
                "y": -0.05,
                "xanchor": "left",
                "yanchor": "top",
            },
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
            if data["edge"] == "PRECEDES" and source in members and target in members:
                dag.add_edge(source, target)

        order = list(nx.topological_sort(dag))
        commits = [(node, store.graph.nodes[node]) for node in order]

        return commits, entities, files

    def _components(
        self,
        store: KnowledgeGraphStore,
        files: list[tuple[str, dict]],
    ) -> dict[str, int]:
        """Union-find over RENAME/ALIAS edges -> {file_node: component_index}."""
        parent: dict[str, str] = {node: node for node, _ in files}

        def find(value: str) -> str:
            while parent[value] != value:
                parent[value] = parent[parent[value]]
                value = parent[value]
            return value

        def union(left: str, right: str) -> None:
            parent[find(left)] = find(right)

        for source, target, data in store.graph.edges(data=True):
            label = data["edge"]
            if label == "ALIAS" and source in parent and target in parent:
                union(source, target)
            elif label == "RENAME":
                origin = data.get("origin")
                if origin in parent and target in parent:
                    union(origin, target)

        roots: dict[str, int] = {}
        result: dict[str, int] = {}
        for node in parent:
            root = find(node)
            if root not in roots:
                roots[root] = len(roots)
            result[node] = roots[root]
        return result

    def _layout(
        self,
        store: KnowledgeGraphStore,
        commits: list[tuple[str, dict]],
        entities: list[tuple[str, dict]],
        files: list[tuple[str, dict]],
        components: dict[str, int],
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
            for source, _, data in store.graph.in_edges(node, data=True):
                if data["edge"] == "RENAME" and source in pos:
                    xs.append(pos[source][0])
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
        files: list[tuple[str, dict]],
        components: dict[str, int],
    ) -> dict[str, dict[int | None, tuple[list, list]]]:
        """Bucket edges into (label -> component -> (xs, ys)).

        Component None is the global bucket for edges unrelated to any file
        component (e.g. PRECEDES between commits). File-linked edges get the
        index of the component the connected file belongs to, so the renderer
        can toggle them via the file-group legend entry.
        """
        result: dict[str, dict[int | None, tuple[list, list]]] = {
            "ADD": {},
            "EDIT": {},
            "DELETE": {},
            "IN": {},
            "PRECEDES": {None: ([], [])},
            "RENAME": {},
            "ALIAS": {},
        }

        entity_component: dict[str, int] = {}
        for node, data in store.graph.nodes(data=True):
            if data.get("kind") not in {"class", "function", "method", "program"}:
                continue
            for _, target, edge in store.graph.out_edges(node, data=True):
                if edge["edge"] == "IN":
                    comp = components.get(target)
                    if comp is not None:
                        entity_component[node] = comp
                    break

        def bucket(label: str, component: int | None) -> tuple[list, list]:
            return result[label].setdefault(component, ([], []))

        for source, target, data in store.graph.edges(data=True):
            if source not in pos or target not in pos:
                continue

            x0, y0 = pos[source]
            x1, y1 = pos[target]
            label = data["edge"]
            source_kind = store.graph.nodes[source]["kind"]
            target_kind = store.graph.nodes[target]["kind"]

            if (
                source_kind == "commit"
                and target_kind in {"class", "function", "method", "program"}
                and label in {"ADD", "EDIT", "DELETE"}
            ):
                comp = entity_component.get(target)
                xs, ys = bucket(label, comp)
                xs.extend([x0, x1, None])
                ys.extend([y0, y1, None])

            if label == "IN":
                comp = components.get(target)
                xs, ys = bucket("IN", comp)
                xs.extend([x0, x1, None])
                ys.extend([y0, y1, None])

            if label == "PRECEDES":
                xs, ys = bucket("PRECEDES", None)
                xs.extend([x0, x1, None])
                ys.extend([y0, y1, None])

            if (
                label == "RENAME"
                and source_kind == "commit"
                and target_kind == "file"
            ):
                comp = components.get(target)
                xs, ys = bucket("RENAME", comp)
                xs.extend([x0, x1, None])
                ys.extend([y0, y1, None])

        file_buckets: dict[int, list[str]] = {}
        for node, _ in files:
            comp = components.get(node)
            if comp is None:
                continue
            file_buckets.setdefault(comp, []).append(node)

        for comp, members in file_buckets.items():
            if len(members) < 2:
                continue
            ordered = sorted(members, key=lambda node: pos[node][0])
            pairs = [
                (pos[left][0], pos[left][1], pos[right][0], pos[right][1])
                for left, right in zip(ordered, ordered[1:])
            ]
            bucket_xs_ys = bucket("ALIAS", comp)
            self._arc(bucket_xs_ys, pairs)

        return result

    def _arc(
        self,
        edge: tuple[list, list],
        pairs: list[tuple[float, float, float, float]],
    ) -> None:
        if not pairs:
            return

        items = [
            {
                "x0": x0,
                "y0": y0,
                "x1": x1,
                "y1": y1,
                "left": min(x0, x1),
                "right": max(x0, x1),
            }
            for x0, y0, x1, y1 in pairs
        ]
        items.sort(key=lambda value: (value["left"], value["right"]))

        lanes: list[float] = []
        for item in items:
            lane = 0
            while lane < len(lanes) and item["left"] < lanes[lane]:
                lane += 1
            if lane == len(lanes):
                lanes.append(item["right"])
            else:
                lanes[lane] = item["right"]
            item["lane"] = lane

        xs, ys = edge
        for item in items:
            span = abs(item["x1"] - item["x0"])
            depth = 0.08 + 0.06 * item["lane"] + min(span * 0.03, 0.12)
            mid = (item["x0"] + item["x1"]) / 2.0
            base = max(item["y0"], item["y1"])
            xs.extend([item["x0"], mid, item["x1"], None])
            ys.extend([item["y0"], base + depth, item["y1"], None])

    def _traces(
        self,
        edges: dict[str, dict[int | None, tuple[list, list]]],
    ) -> list[go.Scatter]:
        style = {
            "ADD": {"width": 1.6, "color": "#2f855a"},
            "EDIT": {"width": 1.6, "color": "#3182ce"},
            "DELETE": {"width": 1.6, "color": "#c53030"},
            "IN": {"width": 1.0, "color": "#bdbdbd"},
            "PRECEDES": {"width": 1.0, "color": "grey"},
            "RENAME": {"width": 1.0, "color": "#d69e2e", "dash": "dot"},
            "ALIAS": {"width": 1.0, "color": "#d69e2e", "dash": "dot"},
        }

        traces = []
        for label, bucket in edges.items():
            line_template = dict(style[label])
            if label == "ALIAS":
                line_template["shape"] = "spline"
                line_template["smoothing"] = 1.15
            for _component, (xs, ys) in bucket.items():
                line = dict(line_template)
                trace_kwargs: dict = {
                    "x": xs,
                    "y": ys,
                    "mode": "lines",
                    "line": line,
                    "hoverinfo": "none",
                    "name": label,
                    "showlegend": False,
                }
                traces.append(go.Scatter(**trace_kwargs))
        return traces

    def _markers(
        self,
        commits: list[tuple[str, dict]],
        entities: list[tuple[str, dict]],
        files: list[tuple[str, dict]],
        pos: dict[str, tuple[float, float]],
        store: KnowledgeGraphStore,
        components: dict[str, int],
    ) -> list[go.Scatter]:
        traces = []
        groups: dict[str, dict[str, list]] = {}

        for node, data in commits:
            payload = data["payload"]
            kind = self._commit_type(payload)
            group = groups.setdefault(
                kind,
                {
                    "x": [],
                    "y": [],
                    "hover": [],
                },
            )
            x, y = pos[node]
            group["x"].append(x)
            group["y"].append(y)
            group["hover"].append(
                f"<b>{kind}</b><br>"
                f"sha: {payload['sha']}<br>"
                f"message: {self._wrap(payload['message'], 180, 48)}"
            )

        for kind in (
            "blame",
            "blame-ancestor",
            "correction-ancestor",
            "correction",
            "candidate",
        ):
            group = groups.get(kind)
            if group is None:
                continue
            traces.append(
                go.Scatter(
                    x=group["x"],
                    y=group["y"],
                    mode="markers",
                    hovertext=group["hover"],
                    hoverinfo="text",
                    marker={
                        "size": 10,
                        "color": self._commit_color(kind),
                        "symbol": "circle",
                        "line": {"width": 1.0, "color": "#2d3748"},
                    },
                    name=kind,
                    showlegend=False,
                )
            )

        titles = self._component_titles(files, components)

        file_members: dict[int, list[tuple[str, dict]]] = {}
        for node, data in files:
            comp = components.get(node)
            if comp is None:
                continue
            file_members.setdefault(comp, []).append((node, data))

        file_of_entity: dict[str, str] = {}
        for node, _ in entities:
            for _, target, data in store.graph.out_edges(node, data=True):
                if data["edge"] == "IN":
                    file_of_entity[node] = target
                    break

        entity_members: dict[int, list[tuple[str, dict]]] = {}
        for node, data in entities:
            file_node = file_of_entity.get(node)
            if file_node is None:
                continue
            comp = components.get(file_node)
            if comp is None:
                continue
            entity_members.setdefault(comp, []).append((node, data))

        for comp in sorted(file_members):
            title = titles.get(comp, f"file-{comp}")
            label = self._truncate(title, 35)
            group_id = f"file-{comp}"

            xs_e = []
            ys_e = []
            hover_e = []
            colors_e = []
            for node, data in entity_members.get(comp, []):
                if node not in pos:
                    continue
                payload = data["payload"]
                x, y = pos[node]
                xs_e.append(x)
                ys_e.append(y)
                hover_e.append(
                    f"<b>{data['kind']}</b><br>"
                    f"name: {payload['name']}<br>"
                    f"path: {payload['path']}<br>"
                    f"urn: {payload['urn']}<br>"
                )
                if data["kind"] == "class":
                    colors_e.append("#ef8a62")
                elif data["kind"] == "method":
                    colors_e.append("#67a9cf")
                elif data["kind"] == "program":
                    colors_e.append("#b2abd2")
                else:
                    colors_e.append("#7bccc4")

            xs_f = []
            ys_f = []
            hover_f = []
            for node, data in file_members[comp]:
                x, y = pos[node]
                xs_f.append(x)
                ys_f.append(y)
                hover_f.append(data["payload"]["path"])

            traces.append(
                go.Scatter(
                    x=xs_f,
                    y=ys_f,
                    mode="markers",
                    hovertext=hover_f,
                    hoverinfo="text",
                    marker={
                        "size": 10,
                        "color": "#2b6cb0",
                        "symbol": "square",
                        "line": {"width": 1.1, "color": "#2d3748"},
                    },
                    name=label,
                    showlegend=False,
                )
            )

            if xs_e:
                traces.append(
                    go.Scatter(
                        x=xs_e,
                        y=ys_e,
                        mode="markers",
                        hovertext=hover_e,
                        hoverinfo="text",
                        marker={
                            "size": 10,
                            "color": colors_e,
                            "symbol": "triangle-up",
                            "line": {"width": 1.0, "color": "#2d3748"},
                        },
                        name=f"{title} entities",
                        showlegend=False,
                    )
                )

        return traces

    def _legend(self) -> list[go.Scatter]:
        entries: list[go.Scatter] = []

        def marker(name: str, color: str, symbol: str, line_width: float) -> None:
            entries.append(
                go.Scatter(
                    x=[None],
                    y=[None],
                    mode="markers",
                    marker={
                        "size": 10,
                        "color": color,
                        "symbol": symbol,
                        "line": {"width": line_width, "color": "#2d3748"},
                    },
                    name=name,
                    legend="legend2",
                    hoverinfo="skip",
                )
            )

        def line(name: str, style: dict) -> None:
            entries.append(
                go.Scatter(
                    x=[None, None],
                    y=[None, None],
                    mode="lines",
                    line=style,
                    name=name,
                    legend="legend2",
                    hoverinfo="skip",
                )
            )

        marker("commit: blame", "#2b6cb0", "circle", 1.0)
        marker("commit: blame-ancestor", "#5dade2", "circle", 1.0)
        marker("commit: correction-ancestor", "#a9d6f5", "circle", 1.0)
        marker("commit: correction", "#e2e8f0", "circle", 1.0)
        marker("entity: class", "#ef8a62", "triangle-up", 1.0)
        marker("entity: method", "#67a9cf", "triangle-up", 1.0)
        marker("entity: function", "#7bccc4", "triangle-up", 1.0)
        marker("entity: program", "#b2abd2", "triangle-up", 1.0)
        marker("file", "#2b6cb0", "square", 1.1)

        line("ADD", {"width": 1.6, "color": "#2f855a"})
        line("EDIT", {"width": 1.6, "color": "#3182ce"})
        line("DELETE", {"width": 1.6, "color": "#c53030"})
        line("IN", {"width": 1.0, "color": "#bdbdbd"})
        line("PRECEDES", {"width": 1.0, "color": "grey"})
        line(
            "RENAME",
            {"width": 1.0, "color": "#d69e2e", "dash": "dot"},
        )
        line(
            "ALIAS",
            {"width": 1.0, "color": "#d69e2e", "dash": "dot", "shape": "spline"},
        )
        return entries

    def _truncate(self, path: str, limit: int) -> str:
        if len(path) <= limit:
            return path
        return "…" + path[-(limit - 1):]

    def _component_titles(
        self,
        files: list[tuple[str, dict]],
        components: dict[str, int],
    ) -> dict[int, str]:
        buckets: dict[int, list[str]] = {}
        for node, data in files:
            comp = components.get(node)
            if comp is None:
                continue
            buckets.setdefault(comp, []).append(data["payload"]["path"])
        return {comp: sorted(paths)[0] for comp, paths in buckets.items()}

    def _commit_type(self, payload: dict) -> str:
        current = payload.get("type")
        if isinstance(current, str):
            kind = current.replace("_", "-")
            if kind != "candidate":
                return kind

        legacy = payload.get("score")
        if legacy == 1.0:
            return "blame"
        if legacy == 0.6:
            return "blame-ancestor"
        if legacy == 0.3:
            return "correction-ancestor"
        if legacy == 0.0:
            return "correction"
        return "candidate"

    def _commit_color(self, kind: str) -> str:
        if kind == "blame":
            return "#2b6cb0"
        if kind == "blame-ancestor":
            return "#5dade2"
        if kind == "correction-ancestor":
            return "#a9d6f5"
        if kind == "correction":
            return "#e2e8f0"
        if kind == "candidate":
            return "#cbd5e0"
        raise ValueError(f"Unsupported commit category {kind}")

    def _wrap(
        self,
        text: str,
        limit: int,
        width: int,
    ) -> str:
        value = html.escape(text[:limit].strip())
        rows: list[str] = []

        for line in value.splitlines():
            rows.extend(
                textwrap.wrap(
                    line,
                    width=width,
                    break_long_words=False,
                    break_on_hyphens=False,
                )
                or [""]
            )

        return "<br>".join(rows)

    def _frame(
        self,
        pos: dict[str, tuple[float, float]],
        edges: dict[str, dict[int | None, tuple[list, list]]],
    ) -> tuple[int, int, dict, dict]:
        xs = [x for x, _ in pos.values()] or [0.0]
        ys = [y for _, y in pos.values()] or [0.0]

        for bucket in edges.values():
            for values, rows in bucket.values():
                xs.extend(value for value in values if value is not None)
                ys.extend(value for value in rows if value is not None)

        lo = (min(xs), min(ys))
        hi = (max(xs), max(ys))

        pad = (0.6, 0.45)

        unit = 40.0
        margin_px = 20 + 14
        data_units = hi[0] - lo[0] + 2 * pad[0]
        width = max(900, int(data_units * unit) + margin_px)
        height = max(360, int((hi[1] - lo[1] + 2 * pad[1]) * 180) + 80)

        plot_px = width - margin_px
        canvas_units = plot_px / unit
        extra = max(0.0, (canvas_units - data_units) / 2.0)

        xaxis = {
            "showgrid": False,
            "zeroline": False,
            "visible": False,
            "range": [lo[0] - pad[0] - extra, hi[0] + pad[0] + extra],
        }
        yaxis = {
            "showgrid": False,
            "zeroline": False,
            "visible": False,
            "range": [lo[1] - pad[1], hi[1] + pad[1]],
        }

        return width, height, xaxis, yaxis
