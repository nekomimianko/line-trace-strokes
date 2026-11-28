#!/usr/bin/env python3
"""Chain variable-radius strokes across junctions with topology cleanup.

Strokes produced by line_trace.py are split at every skeleton node whose
degree != 2, and medial_axis introduces short boundary spurs, H-shaped (Y-Y)
splits at oblique crossings, pull-in distortion, and EDT width inflation blobs
at junctions.

Default mode (v2) addresses these before and during chaining:
1. Iterative radius-aware spur pruning (Scale-Axis / disk-containment) +
   degree-2 node merging, eliminating false boundary hairs while keeping
   real fine details (eyelashes, iris ticks).
2. Junction clustering: contracts short internal bridges whose endpoint
   inscribed disks overlap/touch, recovering true multi-way X-junctions.
3. Extrapolated limb matching from outside the junction distortion zone
   using direction continuity, co-linearity (chord alignment), and width
   compatibility.
4. Smooth cubic Hermite bridging + junction width interpolation across
   paired limbs, plus width-blob clamping and clean snapping for unpaired
   T-junction limbs. Internal bridges are consumed only when covered by the
   reconstructed crossing.
5. Geometric redundancy pruning: drops short strokes whose every sample disk
   is completely covered by longer strokes (near-zero ink loss).

Pass --legacy for the original single-node dot-product chainer.
"""
from __future__ import annotations

import argparse
import json
import math
from collections import defaultdict
from pathlib import Path

import numpy as np
from scipy.spatial import cKDTree


def pt_key(p: dict) -> tuple[float, float]:
    return (round(float(p["x"]), 3), round(float(p["y"]), 3))


def arc_lengths(pts: list[dict]) -> np.ndarray:
    if len(pts) < 2:
        return np.zeros(len(pts), dtype=float)
    xy = np.array([[p["x"], p["y"]] for p in pts], dtype=float)
    seg = np.linalg.norm(np.diff(xy, axis=0), axis=1)
    return np.concatenate([[0.0], np.cumsum(seg)])


def stroke_length(pts: list[dict]) -> float:
    if len(pts) < 2:
        return 0.0
    return float(arc_lengths(pts)[-1])


def dedup_points(pts: list[dict]) -> list[dict]:
    if not pts:
        return pts
    out = [pts[0]]
    for p in pts[1:]:
        if abs(p["x"] - out[-1]["x"]) > 1e-6 or abs(p["y"] - out[-1]["y"]) > 1e-6:
            out.append(p)
    return out


# ------------------------------------------------------------------ legacy

def end_dir(points, side, look=5):
    """Unit vector pointing from the endpoint INTO the stroke (legacy)."""
    if side == "start":
        a, seg = points[0], points[1:look + 1]
        if not seg:
            return None
        b = seg[-1]
    else:
        a, seg = points[-1], points[-look - 1:-1]
        if not seg:
            return None
        b = seg[0]
    vx, vy = b["x"] - a["x"], b["y"] - a["y"]
    n = math.hypot(vx, vy)
    return (vx / n, vy / n) if n > 0 else None


def chain_legacy(strokes: list[dict], max_dot: float = -0.3) -> tuple[list[dict], dict]:
    at = defaultdict(list)
    for s in strokes:
        pts = s["points"]
        if not pts:
            continue
        at[(pts[0]["x"], pts[0]["y"])].append((s["id"], "start"))
        if not s.get("closed") and len(pts) > 1:
            at[(pts[-1]["x"], pts[-1]["y"])].append((s["id"], "end"))

    by_id = {s["id"]: s for s in strokes}
    link = {}
    junctions = 0
    for _coord, ends in at.items():
        if len(ends) < 2:
            continue
        junctions += 1
        dirs = {}
        for e in ends:
            d = end_dir(by_id[e[0]]["points"], e[1])
            if d:
                dirs[e] = d
        cand = []
        el = [e for e in ends if e in dirs]
        for i in range(len(el)):
            for j in range(i + 1, len(el)):
                a, b = el[i], el[j]
                if a[0] == b[0]:
                    continue
                dot = dirs[a][0] * dirs[b][0] + dirs[a][1] * dirs[b][1]
                if dot <= max_dot:
                    cand.append((dot, a, b))
        cand.sort()
        used = set()
        for _dot, a, b in cand:
            if a in used or b in used:
                continue
            used.add(a)
            used.add(b)
            link[a] = b
            link[b] = a

    visited = set()
    chains = []

    def emit(seq):
        pts = []
        closed = False
        for k, (s, rev) in enumerate(seq):
            p = list(reversed(s["points"])) if rev else list(s["points"])
            if k > 0 and pts and p and pts[-1]["x"] == p[0]["x"] and pts[-1]["y"] == p[0]["y"]:
                p = p[1:]
            pts.extend(p)
        if len(seq) == 1 and seq[0][0].get("closed"):
            closed = True
        chains.append({"id": len(chains), "closed": closed, "points": pts})

    for s in strokes:
        if s["id"] in visited or not s["points"]:
            continue
        seq = [(s, False)]
        visited.add(s["id"])
        while True:
            head, hrev = seq[0]
            side = "end" if hrev else "start"
            nxt = link.get((head["id"], side))
            if not nxt or nxt[0] in visited:
                break
            ns = by_id[nxt[0]]
            nrev = (nxt[1] == "start")
            seq.insert(0, (ns, nrev))
            visited.add(ns["id"])
        while True:
            tail, trev = seq[-1]
            side = "start" if trev else "end"
            nxt = link.get((tail["id"], side))
            if not nxt or nxt[0] in visited:
                break
            ns = by_id[nxt[0]]
            nrev = (nxt[1] == "end")
            seq.append((ns, nrev))
            visited.add(ns["id"])
        emit(seq)

    return chains, {"junction_points": junctions, "junction_links": len(link) // 2}


# ------------------------------------------------------------------ v2

def prune_spurs_and_merge(strokes: list[dict],
                          protrusion_px: float = 1.2,
                          max_spur_ratio: float = 2.6,
                          tiny_hair_px: float = 2.5) -> tuple[list[dict], dict]:
    """Iteratively prune medial-axis spurs attached to degree>=3 junctions
    and merge any resulting degree-2 nodes."""
    closed_strokes = []
    edges: dict[int, dict] = {}
    node_edges: dict[tuple[float, float], set[int]] = defaultdict(set)

    next_eid = 0
    for s in strokes:
        pts = dedup_points(list(s["points"]))
        if not pts:
            continue
        if s.get("closed", False) or (len(pts) > 2 and pt_key(pts[0]) == pt_key(pts[-1])):
            closed_strokes.append({"id": -1, "closed": True, "points": pts})
            continue
        if len(pts) == 1:
            closed_strokes.append({"id": -1, "closed": False, "points": pts})
            continue
        u = pt_key(pts[0])
        v = pt_key(pts[-1])
        eid = next_eid
        next_eid += 1
        edges[eid] = {"u": u, "v": v, "points": pts}
        node_edges[u].add(eid)
        node_edges[v].add(eid)

    pruned_spurs = 0
    pruned_parallel = 0
    merged_deg2 = 0

    def remove_edge(eid: int):
        e = edges.pop(eid)
        node_edges[e["u"]].discard(eid)
        node_edges[e["v"]].discard(eid)

    def edge_len(e: dict) -> float:
        return stroke_length(e["points"])

    def is_spur(eid: int) -> tuple[bool, float]:
        e = edges[eid]
        u, v = e["u"], e["v"]
        if u == v:
            return False, 1e9
        du, dv = len(node_edges[u]), len(node_edges[v])
        if du == 1 and dv >= 3:
            pts = e["points"]
            jx, jy, jr = pts[-1]["x"], pts[-1]["y"], pts[-1]["r"]
        elif dv == 1 and du >= 3:
            pts = e["points"]
            jx, jy, jr = pts[0]["x"], pts[0]["y"], pts[0]["r"]
        else:
            return False, 1e9

        L = edge_len(e)
        max_prot = -1e9
        for p in pts:
            d = math.hypot(p["x"] - jx, p["y"] - jy) + p["r"] - jr
            if d > max_prot:
                max_prot = d

        eff_prot = max(protrusion_px, 0.35 * jr)
        if max_prot <= eff_prot and L <= max(4.5, max_spur_ratio * jr):
            return True, max_prot
        if L <= tiny_hair_px and jr >= 1.4 and max_prot <= eff_prot + 0.6:
            return True, max_prot
        return False, max_prot

    for _round in range(25):
        changed = False
        candidates = []
        for eid in list(edges.keys()):
            ok, score = is_spur(eid)
            if ok:
                candidates.append((score, edge_len(edges[eid]), eid))
        candidates.sort()
        for _score, _L, eid in candidates:
            if eid not in edges:
                continue
            ok, _ = is_spur(eid)
            if ok:
                remove_edge(eid)
                pruned_spurs += 1
                changed = True

        pair_map: dict[tuple, list[int]] = defaultdict(list)
        for eid, e in edges.items():
            key = (e["u"], e["v"]) if e["u"] <= e["v"] else (e["v"], e["u"])
            pair_map[key].append(eid)
        for key, eids in pair_map.items():
            if len(eids) >= 2 and key[0] != key[1]:
                eids_sorted = sorted(eids, key=lambda i: edge_len(edges[i]))
                for eid in eids_sorted[:-1]:
                    e = edges[eid]
                    r_end = max(e["points"][0]["r"], e["points"][-1]["r"])
                    if edge_len(e) <= max(4.0, 2.5 * r_end):
                        remove_edge(eid)
                        pruned_parallel += 1
                        changed = True

        deg2_nodes = [n for n, s in list(node_edges.items()) if len(s) == 2]
        for n in deg2_nodes:
            if len(node_edges[n]) != 2:
                continue
            e1_id, e2_id = list(node_edges[n])
            if e1_id == e2_id:
                continue
            e1, e2 = edges[e1_id], edges[e2_id]
            p1 = list(e1["points"])
            if e1["u"] == n:
                p1.reverse()
                other1 = e1["v"]
            else:
                other1 = e1["u"]
            p2 = list(e2["points"])
            if e2["v"] == n:
                p2.reverse()
                other2 = e2["u"]
            else:
                other2 = e2["v"]
            merged_pts = p1 + p2[1:]
            remove_edge(e1_id)
            remove_edge(e2_id)
            if other1 == other2 and len(merged_pts) > 2:
                closed_strokes.append({"id": -1, "closed": True, "points": merged_pts[:-1]})
            else:
                nid = next_eid
                next_eid += 1
                edges[nid] = {"u": other1, "v": other2, "points": merged_pts}
                node_edges[other1].add(nid)
                node_edges[other2].add(nid)
            merged_deg2 += 1
            changed = True

        if not changed:
            break

    result = list(closed_strokes)
    for _eid, e in edges.items():
        result.append({"id": -1, "closed": False, "points": e["points"]})
    for i, s in enumerate(result):
        s["id"] = i

    return result, {
        "pruned_spurs": pruned_spurs,
        "pruned_parallel": pruned_parallel,
        "merged_deg2": merged_deg2,
    }


def sample_limb_geometry(pts: list[dict], side: str, r_junc: float,
                         skip_scale: float = 1.25,
                         window_px: float = 12.0):
    ordered = pts if side == "start" else list(reversed(pts))
    n = len(ordered)
    if n < 2:
        return None
    arc = arc_lengths(ordered)
    total = float(arc[-1])
    if total < 1e-6:
        return None

    skip_px = min(total * 0.45, max(2.0, skip_scale * r_junc))
    if total <= 3.0:
        a_idx = 0
    else:
        a_idx = int(np.searchsorted(arc, skip_px))
        a_idx = min(max(a_idx, 1), n - 2)

    a_arc = arc[a_idx]
    end_idx = int(np.searchsorted(arc, a_arc + window_px))
    end_idx = min(max(end_idx, a_idx + 2), n)
    win = ordered[a_idx:end_idx]
    if len(win) < 2:
        win = ordered[max(0, a_idx - 1):min(n, a_idx + 2)]

    xy = np.array([[p["x"], p["y"]] for p in win], dtype=float)
    w_arc = arc[a_idx:a_idx + len(win)] - a_arc
    w = np.exp(-w_arc / max(4.0, window_px * 0.45))
    mean = (xy * w[:, None]).sum(axis=0) / w.sum()
    diff = xy - mean
    cov = (diff * w[:, None]).T @ diff
    _, eigvecs = np.linalg.eigh(cov)
    vec = eigvecs[:, 1]
    if np.dot(vec, xy[0] - xy[-1]) < 0:
        vec = -vec
    norm = float(np.linalg.norm(vec))
    if norm < 1e-9:
        return None
    dir_to_junc = vec / norm

    rs = np.array([p["r"] for p in win], dtype=float)
    r_clean = float(np.median(rs))

    anchor_pt = ordered[a_idx]
    return {
        "anchor_idx_ordered": a_idx,
        "anchor_xy": np.array([anchor_pt["x"], anchor_pt["y"]], dtype=float),
        "dir_to_junc": dir_to_junc,
        "r_clean": r_clean,
        "total_len": total,
        "junc_xy": np.array([ordered[0]["x"], ordered[0]["y"]], dtype=float),
    }


def hermite_bridge(p0: np.ndarray, d0: np.ndarray, r0: float,
                   p1: np.ndarray, d1: np.ndarray, r1: float,
                   step_px: float = 1.0) -> list[dict]:
    L = float(np.linalg.norm(p1 - p0))
    if L < 1e-6:
        return []
    n_steps = max(2, int(math.ceil(L / step_px)))
    m0 = d0 * L
    m1 = (-d1) * L
    pts = []
    for k in range(1, n_steps):
        t = k / n_steps
        t2 = t * t
        t3 = t2 * t
        pos = ((2 * t3 - 3 * t2 + 1) * p0
               + (t3 - 2 * t2 + t) * m0
               + (-2 * t3 + 3 * t2) * p1
               + (t3 - t2) * m1)
        r = r0 + (r1 - r0) * t
        pts.append({"x": round(float(pos[0]), 3),
                    "y": round(float(pos[1]), 3),
                    "r": round(float(r), 3)})
    return pts


def solve_best_matching(limbs: list, cost_matrix: dict[tuple[int, int], float],
                        max_cost: float) -> list[tuple[int, int]]:
    n = len(limbs)
    if n <= 1:
        return []
    valid_pairs = [(c, i, j) for (i, j), c in cost_matrix.items() if i < j and c <= max_cost]
    if not valid_pairs:
        return []

    if n <= 10:
        bonus = max_cost + 0.35
        adj = defaultdict(list)
        for c, i, j in valid_pairs:
            adj[i].append((j, bonus - c))
            adj[j].append((i, bonus - c))

        best_score = -1.0
        best_pairs: list[tuple[int, int]] = []
        used = [False] * n

        def dfs(idx: int, cur_score: float, cur_pairs: list[tuple[int, int]]):
            nonlocal best_score, best_pairs
            while idx < n and used[idx]:
                idx += 1
            if idx == n:
                if cur_score > best_score:
                    best_score = cur_score
                    best_pairs = list(cur_pairs)
                return
            dfs(idx + 1, cur_score, cur_pairs)
            used[idx] = True
            for j, gain in adj[idx]:
                if j > idx and not used[j]:
                    used[j] = True
                    cur_pairs.append((idx, j))
                    dfs(idx + 1, cur_score + gain, cur_pairs)
                    cur_pairs.pop()
                    used[j] = False
            used[idx] = False

        dfs(0, 0.0, [])
        return best_pairs

    valid_pairs.sort()
    used_s = set()
    out = []
    for _c, i, j in valid_pairs:
        if i in used_s or j in used_s:
            continue
        used_s.add(i)
        used_s.add(j)
        out.append((i, j))
    return out


def cluster_and_chain(strokes: list[dict],
                      bridge_disk_scale: float = 1.0,
                      bridge_margin_px: float = 1.0,
                      max_bridge_px: float = 10.0,
                      max_cluster_diam: float = 18.0,
                      max_angle_deg: float = 55.0,
                      max_chord_deg: float = 45.0,
                      max_r_diff: float = 0.55,
                      clamp_t_blob: bool = True,
                      split_corner_deg: float = 0.0) -> tuple[list[dict], dict]:
    by_id = {s["id"]: s for s in strokes}
    closed_out = []
    open_ids = []
    node_ends: dict[tuple[float, float], list[tuple[int, str]]] = defaultdict(list)

    for s in strokes:
        pts = s["points"]
        if not pts:
            continue
        if s.get("closed", False) or len(pts) == 1:
            closed_out.append({"closed": bool(s.get("closed", False)), "points": list(pts)})
            continue
        u = pt_key(pts[0])
        v = pt_key(pts[-1])
        if u == v:
            closed_out.append({"closed": True, "points": list(pts[:-1]) if len(pts) > 2 else list(pts)})
            continue
        open_ids.append(s["id"])
        node_ends[u].append((s["id"], "start"))
        node_ends[v].append((s["id"], "end"))

    node_r: dict[tuple[float, float], float] = {}
    for node, ends in node_ends.items():
        rs = [by_id[sid]["points"][0 if side == "start" else -1]["r"] for sid, side in ends]
        node_r[node] = max(rs)

    junc_nodes = {n for n, ends in node_ends.items() if len(ends) >= 3}

    bridge_cands = []
    for sid in open_ids:
        pts = by_id[sid]["points"]
        u, v = pt_key(pts[0]), pt_key(pts[-1])
        if u in junc_nodes and v in junc_nodes:
            L = stroke_length(pts)
            lim = min(max_bridge_px, (node_r[u] + node_r[v]) * bridge_disk_scale + bridge_margin_px)
            if L <= lim:
                bridge_cands.append((L, sid, u, v))
    bridge_cands.sort()

    parent = {n: n for n in junc_nodes}
    members = {n: [n] for n in junc_nodes}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    tentative_bridges: dict[tuple[float, float], list[int]] = defaultdict(list)
    all_tentative_set: set[int] = set()
    for _L, sid, u, v in bridge_cands:
        ru, rv = find(u), find(v)
        if ru == rv:
            all_tentative_set.add(sid)
            continue
        comb = members[ru] + members[rv]
        if len(comb) > 10:
            continue
        xs = [p[0] for p in comb]
        ys = [p[1] for p in comb]
        if math.hypot(max(xs) - min(xs), max(ys) - min(ys)) <= max_cluster_diam:
            parent[rv] = ru
            members[ru] = comb
            del members[rv]
            all_tentative_set.add(sid)

    for sid in open_ids:
        pts = by_id[sid]["points"]
        u, v = pt_key(pts[0]), pt_key(pts[-1])
        if u in junc_nodes and v in junc_nodes and find(u) == find(v):
            if sid in all_tentative_set or stroke_length(pts) <= max_bridge_px:
                all_tentative_set.add(sid)
                tentative_bridges[find(u)].append(sid)

    cluster_limbs: dict[tuple[float, float], list[tuple[int, str, tuple[float, float]]]] = defaultdict(list)
    for node, ends in node_ends.items():
        if len(ends) < 2:
            continue
        cid = find(node) if node in junc_nodes else node
        for sid, side in ends:
            if sid in all_tentative_set:
                continue
            cluster_limbs[cid].append((sid, side, node))

    max_angle_rad = math.radians(max_angle_deg)
    max_chord_rad = math.radians(max_chord_deg)
    max_cost = 1.0 * max_angle_rad + 1.2 * max_chord_rad + 0.8 * max_r_diff

    links: dict[tuple[int, str], tuple[int, str, list[dict]]] = {}
    trim_ends: dict[tuple[int, str], int] = {}
    t_clamp_info: dict[tuple[int, str], tuple[float, float, np.ndarray | None]] = {}
    consumed_bridges: set[int] = set()

    multi_clusters = 0
    total_links = 0
    for cid, limbs in cluster_limbs.items():
        c_bridges = tentative_bridges.get(cid, [])
        if len(limbs) < 2:
            continue
        if cid in members and len(members[cid]) > 1:
            multi_clusters += 1

        geoms = [sample_limb_geometry(by_id[sid]["points"], side, node_r[node])
                 for sid, side, node in limbs]

        cost_matrix: dict[tuple[int, int], float] = {}
        for i in range(len(limbs)):
            gi = geoms[i]
            if gi is None:
                continue
            for j in range(i + 1, len(limbs)):
                gj = geoms[j]
                if gj is None or limbs[i][0] == limbs[j][0]:
                    continue
                dot_dir = float(np.clip(np.dot(gi["dir_to_junc"], gj["dir_to_junc"]), -1.0, 1.0))
                theta = math.acos(-dot_dir)
                if theta > max_angle_rad:
                    continue

                chord_vec = gj["anchor_xy"] - gi["anchor_xy"]
                chord_len = float(np.linalg.norm(chord_vec))
                if chord_len > 1e-3:
                    chord_u = chord_vec / chord_len
                    a_i = math.acos(float(np.clip(np.dot(gi["dir_to_junc"], chord_u), -1.0, 1.0)))
                    a_j = math.acos(float(np.clip(np.dot(gj["dir_to_junc"], -chord_u), -1.0, 1.0)))
                    if max(a_i, a_j) > max_chord_rad or 0.5 * (a_i + a_j) > max_chord_rad * 0.85:
                        continue
                    chord_err = 0.5 * (a_i + a_j)
                else:
                    chord_err = 0.0

                ri, rj = gi["r_clean"], gj["r_clean"]
                r_diff = abs(ri - rj) / max(ri, rj, 1.0)
                if r_diff > max_r_diff:
                    continue

                cost = 1.0 * theta + 1.2 * chord_err + 0.8 * r_diff
                cost_matrix[(i, j)] = cost
                cost_matrix[(j, i)] = cost

        pairs = solve_best_matching(limbs, cost_matrix, max_cost)
        paired_indices = set()
        cluster_cover_pts = []

        for i, j in pairs:
            paired_indices.add(i)
            paired_indices.add(j)
            gi, gj = geoms[i], geoms[j]
            sid_i, side_i, _ = limbs[i]
            sid_j, side_j, _ = limbs[j]

            bpts = hermite_bridge(
                gi["anchor_xy"], gi["dir_to_junc"], gi["r_clean"],
                gj["anchor_xy"], gj["dir_to_junc"], gj["r_clean"],
                step_px=1.0,
            )
            for p in bpts:
                cluster_cover_pts.append((p["x"], p["y"], p["r"]))
            cluster_cover_pts.append((float(gi["anchor_xy"][0]), float(gi["anchor_xy"][1]), gi["r_clean"]))
            cluster_cover_pts.append((float(gj["anchor_xy"][0]), float(gj["anchor_xy"][1]), gj["r_clean"]))

            links[(sid_i, side_i)] = (sid_j, side_j, bpts)
            links[(sid_j, side_j)] = (sid_i, side_i, list(reversed(bpts)))
            trim_ends[(sid_i, side_i)] = gi["anchor_idx_ordered"]
            trim_ends[(sid_j, side_j)] = gj["anchor_idx_ordered"]
            total_links += 1

        if pairs and c_bridges:
            for idx, (sid, side, _node) in enumerate(limbs):
                if idx not in paired_indices:
                    pts_l = by_id[sid]["points"]
                    tip = pts_l[0] if side == "start" else pts_l[-1]
                    cluster_cover_pts.append((tip["x"], tip["y"], tip["r"]))
            cov = np.array(cluster_cover_pts, dtype=float)
            cov_xy = cov[:, :2]
            cov_r = cov[:, 2]
            for bsid in c_bridges:
                bxy = np.array([[p["x"], p["y"]] for p in by_id[bsid]["points"]], dtype=float)
                dists = np.linalg.norm(bxy[:, None, :] - cov_xy[None, :, :], axis=2)
                if float((dists - cov_r[None, :]).min(axis=1).max()) <= 1.2:
                    consumed_bridges.add(bsid)

        if clamp_t_blob:
            bridge_xy = (np.array([[p[0], p[1]] for p in cluster_cover_pts], dtype=float)
                         if (pairs and cluster_cover_pts) else None)
            for idx, (sid, side, node) in enumerate(limbs):
                if idx in paired_indices:
                    continue
                g = geoms[idx]
                if g is None:
                    continue
                snap_pt = None
                if bridge_xy is not None and len(bridge_xy) > 0:
                    d_arr = np.linalg.norm(bridge_xy - g["junc_xy"][None, :], axis=1)
                    k_min = int(np.argmin(d_arr))
                    if d_arr[k_min] <= max(2.5, node_r[node] * 1.2):
                        snap_pt = bridge_xy[k_min]
                t_clamp_info[(sid, side)] = (g["r_clean"], float(g["anchor_idx_ordered"]), snap_pt)

    prepared: dict[int, list[dict]] = {}
    for sid in open_ids:
        if sid in consumed_bridges:
            continue
        pts = [dict(p) for p in by_id[sid]["points"]]
        n = len(pts)

        for side in ("start", "end"):
            if (sid, side) in t_clamp_info and (sid, side) not in links:
                r_clean, span_idx, snap_pt = t_clamp_info[(sid, side)]
                span = max(2, int(span_idx) + 1)
                cap = r_clean * 1.12
                if side == "start":
                    for k in range(min(span, n)):
                        if pts[k]["r"] > cap:
                            pts[k]["r"] = round(cap, 3)
                    if snap_pt is not None:
                        d_snap = math.hypot(snap_pt[0] - pts[0]["x"], snap_pt[1] - pts[0]["y"])
                        if 0.2 < d_snap <= 6.0:
                            pts.insert(0, {"x": round(float(snap_pt[0]), 3),
                                           "y": round(float(snap_pt[1]), 3),
                                           "r": round(min(pts[0]["r"], r_clean), 3)})
                            n = len(pts)
                else:
                    for k in range(max(0, n - span), n):
                        if pts[k]["r"] > cap:
                            pts[k]["r"] = round(cap, 3)
                    if snap_pt is not None:
                        d_snap = math.hypot(snap_pt[0] - pts[-1]["x"], snap_pt[1] - pts[-1]["y"])
                        if 0.2 < d_snap <= 6.0:
                            pts.append({"x": round(float(snap_pt[0]), 3),
                                        "y": round(float(snap_pt[1]), 3),
                                        "r": round(min(pts[-1]["r"], r_clean), 3)})
                            n = len(pts)

        t_start = trim_ends.get((sid, "start"), 0)
        t_end = trim_ends.get((sid, "end"), 0)
        if t_start + t_end >= len(pts):
            pts = [pts[len(pts) // 2]]
        else:
            if t_end > 0:
                pts = pts[:len(pts) - t_end]
            if t_start > 0:
                pts = pts[t_start:]
        prepared[sid] = pts

    visited: set[int] = set()
    chains: list[dict] = list(closed_out)

    def emit(seq: list[tuple[int, bool]], bridge_list: list[list[dict]], closed_chain: bool = False):
        pts: list[dict] = []
        for k, (sid, rev) in enumerate(seq):
            p = list(reversed(prepared[sid])) if rev else list(prepared[sid])
            if k > 0:
                pts.extend(bridge_list[k - 1])
            if pts and p and abs(pts[-1]["x"] - p[0]["x"]) < 1e-6 and abs(pts[-1]["y"] - p[0]["y"]) < 1e-6:
                p = p[1:]
            pts.extend(p)
        if closed_chain and len(bridge_list) == len(seq):
            pts.extend(bridge_list[-1])
        pts = dedup_points(pts)
        if pts:
            chains.append({"closed": closed_chain, "points": pts})

    for sid in open_ids:
        if sid in consumed_bridges or sid in visited or sid not in prepared:
            continue
        seq = [(sid, False)]
        bridges_fwd: list[list[dict]] = []
        bridges_bwd: list[list[dict]] = []
        visited.add(sid)
        is_closed_loop = False

        while True:
            head_id, hrev = seq[0]
            head_side = "end" if hrev else "start"
            lk = links.get((head_id, head_side))
            if not lk:
                break
            nxt_id, nxt_side, bpts = lk
            if nxt_id in visited:
                tail_id, trev = seq[-1]
                tail_side = "start" if trev else "end"
                if nxt_id == tail_id and nxt_side == tail_side and len(seq) >= 2:
                    is_closed_loop = True
                    bridges_fwd.append(list(reversed(bpts)))
                break
            seq.insert(0, (nxt_id, nxt_side == "start"))
            bridges_bwd.insert(0, list(reversed(bpts)))
            visited.add(nxt_id)

        if not is_closed_loop:
            while True:
                tail_id, trev = seq[-1]
                tail_side = "start" if trev else "end"
                lk = links.get((tail_id, tail_side))
                if not lk:
                    break
                nxt_id, nxt_side, bpts = lk
                if nxt_id in visited:
                    head_id, hrev = seq[0]
                    head_side = "end" if hrev else "start"
                    if nxt_id == head_id and nxt_side == head_side and len(seq) >= 2:
                        is_closed_loop = True
                        bridges_fwd.append(bpts)
                    break
                seq.append((nxt_id, nxt_side == "end"))
                bridges_fwd.append(bpts)
                visited.add(nxt_id)

        emit(seq, bridges_bwd + bridges_fwd, closed_chain=is_closed_loop)

    n_corner_splits = 0
    if split_corner_deg > 0:
        split_chains = []
        for c in chains:
            pts = c["points"]
            if c["closed"] or len(pts) < 7:
                split_chains.append(c)
                continue
            xy = np.array([[p["x"], p["y"]] for p in pts], dtype=float)
            arc = arc_lengths(pts)
            n = len(pts)
            angles = np.zeros(n)
            for i in range(2, n - 2):
                ia = int(np.searchsorted(arc, arc[i] - 4.0))
                ib = min(n - 1, int(np.searchsorted(arc, arc[i] + 4.0)))
                if ia >= i or ib <= i:
                    continue
                v1 = xy[i] - xy[ia]
                v2 = xy[ib] - xy[i]
                n1, n2 = np.linalg.norm(v1), np.linalg.norm(v2)
                if n1 >= 1.5 and n2 >= 1.5:
                    angles[i] = math.degrees(math.acos(float(np.clip(np.dot(v1, v2) / (n1 * n2), -1.0, 1.0))))
            cuts = []
            i = 2
            while i < n - 2:
                if angles[i] >= split_corner_deg:
                    j = i
                    best_i = i
                    while j < n - 2 and angles[j] >= split_corner_deg:
                        if angles[j] > angles[best_i]:
                            best_i = j
                        j += 1
                    if not cuts or (arc[best_i] - arc[cuts[-1]]) >= 5.0:
                        cuts.append(best_i)
                    i = j
                else:
                    i += 1
            if not cuts:
                split_chains.append(c)
            else:
                bounds = [0] + cuts + [n - 1]
                for a, b in zip(bounds[:-1], bounds[1:]):
                    if b - a >= 1:
                        split_chains.append({"closed": False, "points": pts[a:b + 1]})
                n_corner_splits += len(cuts)
        chains = split_chains

    for i, c in enumerate(chains):
        c["id"] = i

    return chains, {
        "consumed_bridges": len(consumed_bridges),
        "multi_node_clusters": multi_clusters,
        "junction_links": total_links,
        "corner_splits": n_corner_splits,
    }


def prune_redundant_covered(strokes: list[dict],
                            max_len_px: float = 14.0,
                            cover_margin_px: float = 0.85) -> tuple[list[dict], int]:
    """Drop short open strokes whose every disk is geometrically covered by
    longer surviving strokes (near-zero ink loss)."""
    n = len(strokes)
    if n <= 1:
        return strokes, 0

    lens = [stroke_length(s["points"]) for s in strokes]
    all_xy, all_r, all_sid = [], [], []
    for sid, s in enumerate(strokes):
        for p in s["points"]:
            all_xy.append((p["x"], p["y"]))
            all_r.append(p["r"])
            all_sid.append(sid)
    if not all_xy:
        return strokes, 0

    xy_arr = np.array(all_xy, dtype=float)
    r_arr = np.array(all_r, dtype=float)
    sid_arr = np.array(all_sid, dtype=int)
    tree = cKDTree(xy_arr)
    max_line_r = min(float(r_arr.max()), 12.0)

    alive = [True] * n
    order = sorted(
        (i for i in range(n) if (not strokes[i].get("closed", False)) and lens[i] <= max_len_px),
        key=lambda i: (lens[i], len(strokes[i]["points"])),
    )
    removed = 0
    for sid in order:
        pts = strokes[sid]["points"]
        if not pts:
            alive[sid] = False
            removed += 1
            continue
        all_cov = True
        for p in pts:
            px, py, pr = p["x"], p["y"], p["r"]
            if pr > 12.0:
                all_cov = False
                break
            idxs = tree.query_ball_point((px, py), max_line_r + pr + cover_margin_px + 0.5)
            covered = False
            for idx in idxs:
                osid = int(sid_arr[idx])
                if osid == sid or not alive[osid] or lens[osid] < lens[sid] + 2.0:
                    continue
                if math.hypot(px - xy_arr[idx, 0], py - xy_arr[idx, 1]) + pr <= r_arr[idx] + cover_margin_px:
                    covered = True
                    break
            if not covered:
                all_cov = False
                break
        if all_cov:
            alive[sid] = False
            removed += 1

    kept = [s for i, s in enumerate(strokes) if alive[i]]
    for i, s in enumerate(kept):
        s["id"] = i
    return kept, removed


def main(argv=None):
    ap = argparse.ArgumentParser(description="Chain variable-radius strokes across junctions")
    ap.add_argument("json_file", type=Path)
    ap.add_argument("-o", "--output", type=Path, default=Path("strokes_chained.json"))
    ap.add_argument("--legacy", action="store_true",
                    help="use original single-node dot-product chainer (no spur prune or clustering)")
    ap.add_argument("--max-dot", type=float, default=-0.3,
                    help="(legacy) pair ends only if dot(dir_in_a, dir_in_b) <= this")
    ap.add_argument("--protrusion", type=float, default=1.2,
                    help="max protrusion px beyond junction disk for spur pruning")
    ap.add_argument("--max-angle", type=float, default=55.0,
                    help="max crossing angle deviation deg for limb pairing")
    ap.add_argument("--max-chord", type=float, default=45.0,
                    help="max chord misalignment deg for limb pairing")
    ap.add_argument("--max-r-diff", type=float, default=0.55,
                    help="max relative radius difference for limb pairing")
    ap.add_argument("--cover-margin", type=float, default=0.85,
                    help="margin px for covered redundant short-stroke pruning (<=0 disables)")
    ap.add_argument("--split-corner-deg", type=float, default=0.0,
                    help="split open strokes at sharp corners >= this deg (0 = off)")
    ap.add_argument("--no-prune", action="store_true",
                    help="skip pre-chaining spur pruning")
    args = ap.parse_args(argv)

    data = json.loads(args.json_file.read_text(encoding="utf-8"))
    strokes = data["strokes"]
    n_in = len(strokes)
    pts_in = sum(len(s["points"]) for s in strokes)

    if args.legacy:
        chains, stats = chain_legacy(strokes, max_dot=args.max_dot)
    else:
        if not args.no_prune:
            strokes, pstats = prune_spurs_and_merge(strokes, protrusion_px=args.protrusion)
        else:
            pstats = {"pruned_spurs": 0, "pruned_parallel": 0, "merged_deg2": 0}
        n_after_prune = len(strokes)
        chains, cstats = cluster_and_chain(
            strokes,
            max_angle_deg=args.max_angle,
            max_chord_deg=args.max_chord,
            max_r_diff=args.max_r_diff,
            split_corner_deg=args.split_corner_deg,
        )
        if args.cover_margin > 0:
            chains, n_cov = prune_redundant_covered(chains, cover_margin_px=args.cover_margin)
        else:
            n_cov = 0
        stats = {
            "stroke_count_after_prune": n_after_prune,
            "pruned_covered_redundant": n_cov,
            **pstats,
            **cstats,
        }

    out = dict(data)
    out["strokes"] = chains
    out["stats"] = dict(data.get("stats", {}))
    out["stats"].update({
        "stroke_count_before_chaining": n_in,
        "stroke_count": len(chains),
        "exported_points": sum(len(c["points"]) for c in chains),
        **stats,
    })
    args.output.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

    before = np.array([len(s["points"]) for s in data["strokes"]])
    after = np.array([len(c["points"]) for c in chains]) if chains else np.array([0])
    if args.legacy:
        print(f"strokes: {n_in} -> {len(chains)}  (legacy junctions {stats['junction_points']}, links {stats['junction_links']})")
    else:
        print(
            f"strokes: {n_in} -> prune {stats['stroke_count_after_prune']} "
            f"(spurs={stats['pruned_spurs']}, par={stats['pruned_parallel']}, deg2={stats['merged_deg2']}) "
            f"-> chain {len(chains) + stats['pruned_covered_redundant']} "
            f"(bridges={stats['consumed_bridges']}, multi_clusters={stats['multi_node_clusters']}, "
            f"links={stats['junction_links']}) -> cov_prune {len(chains)} (dropped={stats['pruned_covered_redundant']})"
        )
    print(f"length median: {np.median(before):.0f} -> {np.median(after):.0f} pts, "
          f"mean {before.mean():.1f} -> {after.mean():.1f}, max {before.max()} -> {after.max()}")
    print(f"total points: {pts_in} -> {out['stats']['exported_points']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
