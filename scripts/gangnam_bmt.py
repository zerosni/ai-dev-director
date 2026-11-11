#!/usr/bin/env python3
"""Gangnam-gu (11680) PyQGIS benchmark: CRS accuracy, topology, spatial-index speedup, buffer validity.

Runs headless (python-qgis / OSGeo4W shell) or inside the QGIS Python console. Synthetic data only, so it needs
no network and no credentials. Writes benchmark_report.json and exits 0 (pass) / 1 (fail) / 2 (QGIS missing).

    python scripts/gangnam_bmt.py --buildings 10000 --points 5000 --seed 42 --out benchmark_report.json
"""
import argparse
import json
import math
import os
import random
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from qgis.core import (QgsApplication, QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsFeature,
                           QgsGeometry, QgsPointXY, QgsProject, QgsRectangle, QgsSpatialIndex, Qgis)
except ImportError:
    print(json.dumps({"passed": False, "error": "QGIS_PYTHON_NOT_FOUND",
                      "hint": "Run from the OSGeo4W Shell / QGIS Python console (qgis.core must be importable)."}))
    sys.exit(2)

# Gangnam-gu bounding box (lon/lat, EPSG:4326) and target projected CRS (Korea 2000 Unified CS, GRS80).
LON_MIN, LON_MAX, LAT_MIN, LAT_MAX = 127.01, 127.14, 37.46, 37.54
TARGET_EPSG = "EPSG:5179"

# Pass criteria (tune in docs/GANGNAM_BMT.md).
MAX_CRS_ROUNDTRIP_MM = 1.0
MIN_INDEX_SPEEDUP = 10.0
SLIVER_THINNESS = 0.02          # 4*pi*A/P^2 below this => sliver-like


def rss_mb():
    try:
        import psutil
        return round(psutil.Process().memory_info().rss / 1048576, 1)
    except Exception:
        return None


def ms_since(t0):
    return round((time.perf_counter() - t0) * 1000, 2)


def bbox_in_target():
    ctx = QgsProject.instance().transformContext()
    tr = QgsCoordinateTransform(QgsCoordinateReferenceSystem("EPSG:4326"),
                                QgsCoordinateReferenceSystem(TARGET_EPSG), ctx)
    return tr.transformBoundingBox(QgsRectangle(LON_MIN, LAT_MIN, LON_MAX, LAT_MAX)), tr


def crs_roundtrip(rng, box, n=2000):
    """5179 -> 4326 -> 5179 residual in millimetres (self-consistency of the transform pipeline).

    This is NOT an absolute accuracy test against survey control points; it detects broken/low-precision
    transform chains. Bessel(EPSG:5174) <-> GRS80(EPSG:5179) needs a datum-shift grid and reference points.
    """
    ctx = QgsProject.instance().transformContext()
    fwd = QgsCoordinateTransform(QgsCoordinateReferenceSystem(TARGET_EPSG), QgsCoordinateReferenceSystem("EPSG:4326"), ctx)
    back = QgsCoordinateTransform(QgsCoordinateReferenceSystem("EPSG:4326"), QgsCoordinateReferenceSystem(TARGET_EPSG), ctx)
    errs = []
    for _ in range(n):
        p = QgsPointXY(rng.uniform(box.xMinimum(), box.xMaximum()), rng.uniform(box.yMinimum(), box.yMaximum()))
        q = back.transform(fwd.transform(p))
        errs.append(math.hypot(q.x() - p.x(), q.y() - p.y()) * 1000.0)
    errs.sort()
    return {"samples": n, "mean_mm": round(sum(errs) / n, 6), "p99_mm": round(errs[int(n * .99) - 1], 6),
            "max_mm": round(errs[-1], 6)}


def make_buildings(rng, box, n, bad_ratio=0.01):
    """n rotated rectangles. A small known share is deliberately defective so the detectors are proven to work:
    bow-ties (invalid), exact duplicates and slivers."""
    geoms, injected = [], {"bowtie": 0, "duplicate": 0, "sliver": 0}
    for i in range(n):
        cx, cy = rng.uniform(box.xMinimum(), box.xMaximum()), rng.uniform(box.yMinimum(), box.yMaximum())
        w, h, a = rng.uniform(8, 40), rng.uniform(8, 40), rng.uniform(0, math.pi)
        kind = None
        if rng.random() < bad_ratio:
            kind = rng.choice(("bowtie", "duplicate", "sliver"))
        if kind == "duplicate" and geoms:
            geoms.append(QgsGeometry(geoms[rng.randrange(len(geoms))]))
            injected["duplicate"] += 1
            continue
        if kind == "sliver":
            h = 0.05
        c, s = math.cos(a), math.sin(a)
        pts = [(-w / 2, -h / 2), (w / 2, -h / 2), (w / 2, h / 2), (-w / 2, h / 2)]
        if kind == "bowtie":
            pts = [pts[0], pts[2], pts[1], pts[3]]
        ring = [QgsPointXY(cx + x * c - y * s, cy + x * s + y * c) for x, y in pts]
        ring.append(ring[0])
        geoms.append(QgsGeometry.fromPolygonXY([ring]))
        if kind:
            injected[kind] += 1
    return geoms, injected


def make_points(rng, box, n):
    return [QgsGeometry.fromPointXY(QgsPointXY(rng.uniform(box.xMinimum(), box.xMaximum()),
                                              rng.uniform(box.yMinimum(), box.yMaximum()))) for _ in range(n)]


def make_roads(rng, box, n):
    out = []
    for _ in range(n):
        x, y = rng.uniform(box.xMinimum(), box.xMaximum()), rng.uniform(box.yMinimum(), box.yMaximum())
        pts = [QgsPointXY(x, y)]
        for _ in range(rng.randint(2, 6)):
            x += rng.uniform(-300, 300)
            y += rng.uniform(-300, 300)
            pts.append(QgsPointXY(x, y))
        out.append(QgsGeometry.fromPolylineXY(pts))
    return out


def topology_defects(geoms):
    invalid = sum(1 for g in geoms if not g.isGeosValid())
    seen, dup = set(), 0
    for g in geoms:
        key = bytes(g.asWkb())
        dup += key in seen
        seen.add(key)
    sliver = 0
    for g in geoms:
        a, p = g.area(), g.length()
        if p > 0 and 4 * math.pi * a / (p * p) < SLIVER_THINNESS:
            sliver += 1
    n = max(len(geoms), 1)
    return {"invalid": invalid, "duplicates": dup, "slivers": sliver,
            "defect_rate_pct": round(100.0 * (invalid + dup + sliver) / n, 3)}


def join_without_index(points, buildings, sample):
    sub = points[:sample]
    t0 = time.perf_counter()
    hits = [sum(1 for b in buildings if p.intersects(b)) for p in sub]
    ms = ms_since(t0)
    return ms, hits


def join_with_index(points, buildings, sample=None):
    t0 = time.perf_counter()
    idx = QgsSpatialIndex()
    for i, b in enumerate(buildings):
        f = QgsFeature(i)
        f.setGeometry(b)
        idx.addFeature(f)
    build_ms = ms_since(t0)
    sub = points if sample is None else points[:sample]
    t1 = time.perf_counter()
    hits = []
    for p in sub:
        pt = p.asPoint()
        cands = idx.intersects(QgsRectangle(pt.x(), pt.y(), pt.x(), pt.y()))
        hits.append(sum(1 for i in cands if p.intersects(buildings[i])))
    return build_ms, ms_since(t1), hits, idx


def buffer_join(roads, buildings, idx, dist=15.0):
    """Road buffer (dist m) x building polygon spatial join through the R-tree."""
    t0 = time.perf_counter()
    joined = 0
    for r in roads:
        buf = r.buffer(dist, 8)
        cands = idx.intersects(buf.boundingBox())
        joined += sum(1 for i in cands if buf.intersects(buildings[i]))
    return ms_since(t0), joined


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--buildings", type=int, default=10000)
    ap.add_argument("--points", type=int, default=5000)
    ap.add_argument("--roads", type=int, default=500)
    ap.add_argument("--noindex-sample", type=int, default=300, help="points used for the slow no-index run (extrapolated)")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--out", default="benchmark_report.json")
    a = ap.parse_args()

    standalone = QgsApplication.instance() is None   # inside the QGIS console an app already exists
    qgs = None
    if standalone:
        qgs = QgsApplication([], False)
        qgs.initQgis()

    rng = random.Random(a.seed)
    mem0 = rss_mb()
    box, _ = bbox_in_target()
    report = {"region": "Seoul Gangnam-gu (11680)", "crs": TARGET_EPSG, "qgis": Qgis.version(), "seed": a.seed,
              "bbox_5179": [round(box.xMinimum(), 1), round(box.yMinimum(), 1), round(box.xMaximum(), 1), round(box.yMaximum(), 1)],
              "data": "synthetic", "counts": {"buildings": a.buildings, "points": a.points, "roads": a.roads}}

    report["platform_quality"] = {"crs_roundtrip": crs_roundtrip(rng, box)}
    buildings, injected = make_buildings(rng, box, a.buildings)
    points, roads = make_points(rng, box, a.points), make_roads(rng, box, a.roads)
    mem_data = rss_mb()

    defects = topology_defects(buildings)
    defects["injected"] = injected
    defects["detected_all_injected_invalid"] = defects["invalid"] >= injected["bowtie"]
    report["platform_quality"]["topology"] = defects

    sample = min(a.noindex_sample, a.points)
    noidx_ms, noidx_hits = join_without_index(points, buildings, sample)
    build_ms, idx_ms_full, idx_hits_full, idx = join_with_index(points, buildings)
    idx_sample_hits = idx_hits_full[:sample]
    noidx_full_est = noidx_ms * a.points / max(sample, 1)
    mem_index = rss_mb()
    rd_ms, rd_joined = buffer_join(roads, buildings, idx)

    valid_in = [b for b in buildings if b.isGeosValid()]
    t0 = time.perf_counter()
    buffered = [g.buffer(5.0, 8) for g in valid_in]
    buf_ms = ms_since(t0)
    buf_invalid = sum(1 for g in buffered if not g.isGeosValid())

    speedup = round(noidx_full_est / max(idx_ms_full + build_ms, 0.001), 1)
    report["analysis_performance"] = {
        "point_in_building_no_index_ms_sample": noidx_ms, "no_index_sample_points": sample,
        "point_in_building_no_index_ms_extrapolated": round(noidx_full_est, 1),
        "rtree_build_ms": build_ms, "point_in_building_indexed_ms": idx_ms_full,
        "speedup_x": speedup, "results_identical_on_sample": idx_sample_hits == noidx_hits,
        "road_buffer15m_x_building_join_ms": rd_ms, "road_buffer_join_pairs": rd_joined,
        "building_buffer5m_ms": buf_ms,
        "memory_rss_mb": {"start": mem0, "after_data": mem_data, "after_index": mem_index}}
    report["geometry_validity_after_buffer"] = {"input_valid": len(valid_in), "buffered": len(buffered),
                                                "invalid_after_buffer": buf_invalid}
    report["not_measured"] = ["raster tile latency", "zonal statistics (need a DEM/orthophoto and network)",
                              "absolute CRS accuracy vs survey control points"]

    checks = {
        "crs_roundtrip_max_mm<=%s" % MAX_CRS_ROUNDTRIP_MM: report["platform_quality"]["crs_roundtrip"]["max_mm"] <= MAX_CRS_ROUNDTRIP_MM,
        "invalid_detector_finds_injected": defects["detected_all_injected_invalid"],
        "index_results_identical": report["analysis_performance"]["results_identical_on_sample"],
        "index_speedup>=%sx" % MIN_INDEX_SPEEDUP: speedup >= MIN_INDEX_SPEEDUP,
        "no_invalid_after_buffer": buf_invalid == 0}
    report["checks"] = checks
    report["passed"] = all(checks.values())

    text = json.dumps(report, ensure_ascii=False, indent=2)
    with open(a.out, "w", encoding="utf-8") as fh:
        fh.write(text)
    print(text)
    if qgs:
        qgs.exitQgis()
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
