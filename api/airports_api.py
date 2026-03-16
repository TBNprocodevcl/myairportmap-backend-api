from flask import Blueprint, jsonify, request
import pandas as pd

from app import get_visited_norm_ids, load_airports_cached, normalize_id

airports_api = Blueprint("airports_api", __name__)

@airports_api.route("/api/airports", methods=["GET"])
def api_airports():
    """
    Returns GeoJSON for airports in viewport bbox.

    Query:
      bbox=west,south,east,north   (required)
      unvisited=1                 (optional; exclude visited for handle)
      handle=<handle>             (optional; used only if unvisited=1)
      zoom=<int>                  (optional; Leaflet zoom)
      limit=<int>                 (optional; hard cap when zoomed out)

    Map38 rules:
      ✅ Airports come ONLY from canonical load_airports_cached()
      ✅ Visits are only read when unvisited=1 and handle is provided
      ✅ Unvisited is complete at closer zooms (no sampling), capped only when zoomed out
    """
    import hashlib
    import math

    # -----------------------------
    # Parse inputs
    # -----------------------------
    bbox = (request.args.get("bbox") or "").strip()
    if not bbox:
        return jsonify({"type": "FeatureCollection", "features": [], "meta": {"reason": "no_bbox"}})

    try:
        west, south, east, north = [float(x) for x in bbox.split(",")]
    except Exception:
        return jsonify({"type": "FeatureCollection", "features": [], "meta": {"reason": "bad_bbox"}})

    # Dateline not supported (fine for CONUS)
    if west > east:
        return jsonify({"type": "FeatureCollection", "features": [], "meta": {"reason": "dateline"}})

    unvisited = (request.args.get("unvisited") == "1")
    handle = (request.args.get("handle") or "").strip() or None

    # zoom is optional
    z_raw = (request.args.get("zoom") or "").strip()
    zoom = None
    try:
        if z_raw:
            zoom = int(float(z_raw))
    except Exception:
        zoom = None

    # user-provided cap (used only when zoomed out; otherwise ignored)
    try:
        limit = int(request.args.get("limit") or "2500")
        limit = max(50, min(limit, 5000))
    except Exception:
        limit = 2500

    # -----------------------------
    # Defensive: total must always exist
    # -----------------------------
    total = 0
   
    # -----------------------------
    # Zoom policy for UNVISITED overlay
    # -----------------------------
    # - zoom >= 7: return ALL in bbox (no sampling)
    # - zoom == 6: cap to 2000
    # - zoom <= 5: cap to 1200
    effective_limit = limit
    full_mode = False
    if unvisited and (zoom is not None):
        if zoom >= 7:
            full_mode = True
            effective_limit = 5000  # > entire universe (~3270), effectively "no cap"
        elif zoom >= 6:
            effective_limit = min(effective_limit, 2000)
        else:
            effective_limit = min(effective_limit, 1200)

    # -----------------------------
    # ✅ Canonical airports ONLY
    # -----------------------------
    try:
        df_airports = load_airports_cached()
    except Exception as e:
        return jsonify({
            "type": "FeatureCollection",
            "features": [],
            "meta": {"reason": "airports_load_failed", "err": str(e)}
        })

    # Ensure norm_id exists (for visited filtering)
    if "norm_id" not in df_airports.columns:
        try:
            df_airports = df_airports.copy()
            df_airports["norm_id"] = df_airports["airport_id"].astype(str).apply(normalize_id)
        except Exception:
            pass

    # -----------------------------
    # BBox filter (canonical lat/long)
    # -----------------------------
    df = df_airports[
        (pd.to_numeric(df_airports["lat"], errors="coerce") >= south) &
        (pd.to_numeric(df_airports["lat"], errors="coerce") <= north) &
        (pd.to_numeric(df_airports["long"], errors="coerce") >= west) &
        (pd.to_numeric(df_airports["long"], errors="coerce") <= east)
    ].copy()

    # ✅ Total in bbox (before any unvisited filtering / sampling)
    total = int(len(df))

    # -----------------------------
    # Exclude visited if requested (cached visited set)
    # -----------------------------
    if unvisited and handle:
        try:
            visited = get_visited_norm_ids(handle)
            if "norm_id" in df.columns and visited:
                df = df[~df["norm_id"].astype(str).isin(visited)]
        except Exception:
            pass

        # ✅ Total after unvisited filtering (this is the population for sampling/cap)
        total = int(len(df))

    # -----------------------------
    # LOD sampling (only when zoomed out AND over cap)
    # -----------------------------
    # Goal: show "some" airports at low zoom (so toggle doesn't feel broken),
    # while keeping points spatially even and stable (no random flicker).
    #
    # Approach: deterministic "grid bucket" sampling keyed to zoom.
    #
    # We only apply this if NOT full_mode and total > effective_limit.
    sampled = False
    cell_miles = None
    capped = False

    def _cell_miles_for_zoom(z: int | None) -> float | None:
        # Tune these values if needed; conservative defaults.
        if z is None:
            return 120.0
        if z <= 4:
            return 140.0
        if z == 5:
            return 80.0
        if z == 6:
            return 40.0
        # z>=7 => full_mode already handles 7+, but keep consistent:
        return None

    def _grid_sample(df_in: pd.DataFrame, mid_lat: float, cell_miles_in: float) -> pd.DataFrame:
        df0 = df_in.copy()

        # numeric once
        df0["__lat"] = pd.to_numeric(df0["lat"], errors="coerce")
        df0["__lon"] = pd.to_numeric(df0["long"], errors="coerce")
        df0 = df0.dropna(subset=["__lat", "__lon"])
        if df0.empty:
            return df0

        lat_step = cell_miles_in / 69.0
        coslat = max(0.2, math.cos(math.radians(mid_lat)))
        lon_step = cell_miles_in / (69.0 * coslat)

        lat_bin = (df0["__lat"] / lat_step).astype(int)
        lon_bin = (df0["__lon"] / lon_step).astype(int)
        df0["__bin"] = lat_bin.astype(str) + ":" + lon_bin.astype(str)

        # deterministic pick per bin
        if "airport_id" in df0.columns:
            df0 = df0.sort_values(["__bin", "airport_id"], kind="mergesort")
        else:
            df0 = df0.sort_values(["__bin", "name"], kind="mergesort")

        df0 = df0.drop_duplicates("__bin", keep="first")
        return df0.drop(columns=["__lat", "__lon", "__bin"])

    if (not full_mode) and (total > effective_limit):
        # Apply spatial LOD sampling first (stable)
        cell_miles = _cell_miles_for_zoom(zoom)
        if cell_miles is not None:
            sampled = True
            mid_lat = (south + north) / 2.0
            df = _grid_sample(df, mid_lat=mid_lat, cell_miles_in=cell_miles)

        # After grid sampling, still enforce hard cap as a backstop
        if len(df) > effective_limit:
            capped = True
            # deterministic trim: stable order
            if "airport_id" in df.columns:
                df = df.sort_values(["airport_id"], kind="mergesort").head(effective_limit).copy()
            else:
                df = df.sort_values(["name"], kind="mergesort").head(effective_limit).copy()

    total_after = int(len(df))

    # -----------------------------
    # Build GeoJSON (properties match current JS: expects boolean 'towered')
    # -----------------------------
    features = []
    for _, r in df.iterrows():
        try:
            lon = float(r["long"])
            lat = float(r["lat"])
        except Exception:
            continue

        status = str(r.get("towered_status") or "")
        is_towered = status.lower().startswith("towered")

        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [lon, lat]},
            "properties": {
                "airport_id": str(r.get("airport_id") or ""),
                "name": str(r.get("name") or ""),
                "state": str(r.get("state") or ""),
                "towered": bool(is_towered),
                "towered_status": status,
            }
        })

    return jsonify({
        "type": "FeatureCollection",
        "features": features,
        "meta": {
            "total_in_bbox": total,
            "total_after_sampling": total_after,
            "returned": len(features),
            "limit": effective_limit,
            "zoom": zoom,

            # Back-compat: keep existing key name
            "unvisited": bool(unvisited and handle),

            # ✅ added clarity
            "unvisited_requested": bool(unvisited),
            "unvisited_applied": bool(unvisited and handle),
            "handle_present": bool(handle),

            "source": "canonical_airports_csv",
            "sampled": bool(sampled),
            "cell_miles": cell_miles,
            "capped": bool(capped),
            "full_mode": bool(full_mode),
        }
    })
