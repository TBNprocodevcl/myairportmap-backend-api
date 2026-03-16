from flask import Blueprint, jsonify, request
import pandas as pd

from app import get_handle_for_user, load_data, login_required, resolve_visits_csv


airports_api = Blueprint("airports_api", __name__)

@airports_api.route("/api/visits")
@login_required
def route_api_visits():
    """
    BBOX-driven visits loader for progressive map hydration.

    Query:
      - bbox=west,south,east,north  (required)
      - handle=<handle>            (required)
      - mode=first|all             (required)
      - limit=<int>                (optional, default 400)
      - cursor=<int>               (optional, default 0)

    Returns:
      { "items": [ {id, lat, lon, towered, popup_html} ... ],
        "next_cursor": <int or null>,
        "meta": {...}
      }
    """
    try:
        claims = getattr(request, "clerk_claims", {}) or {}
        user_id = (claims.get("sub") or "").strip()

        if not user_id:
            return jsonify({"error": "Unauthorized"}), 401

        handle = get_handle_for_user(user_id)
        if not handle:
            return jsonify({"error": "Handle not found"}), 404

        bbox = (request.args.get("bbox") or "").strip()
        mode = (request.args.get("mode") or "").strip().lower()

        if not bbox or mode not in {"first", "all"}:
            return jsonify({"items": [], "next_cursor": None, "meta": {"error": "missing params"}}), 400

        parts = [p.strip() for p in bbox.split(",")]
        if len(parts) != 4:
            return jsonify({"items": [], "next_cursor": None, "meta": {"error": "invalid bbox"}}), 400

        west, south, east, north = [float(x) for x in parts]

        # limit/cursor (bounded)
        try:
            limit = int(request.args.get("limit") or "400")
        except Exception:
            limit = 400
        if limit < 50:
            limit = 50
        if limit > 1200:
            limit = 1200

        try:
            cursor = int(request.args.get("cursor") or "0")
        except Exception:
            cursor = 0
        if cursor < 0:
            cursor = 0

        # Load data (your load_data is fast per your MAP_TIMING logs)
        visits_csv = resolve_visits_csv(handle)
        df_airports, df_visits = load_data(visits_csv=visits_csv, handle=handle)

        if df_visits is None or df_visits.empty or df_airports is None or df_airports.empty:
            return jsonify({"items": [], "next_cursor": None, "meta": {"count": 0}})

        # --- Canonical tower status (same logic as map) ---
        def _canon_towered_status(v) -> str:
            s = str(v or "").strip().lower()
            if not s or s in {"nan", "none", "null"}:
                return "Non-Towered"
            if ("non" in s and "tower" in s) or ("no tower" in s):
                return "Non-Towered"
            if s in {"no", "n", "false", "0", "uncontrolled", "ctaf", "untowered", "none"}:
                return "Non-Towered"
            if s in {"towered", "twr", "yes", "y", "true", "1", "controlled", "ct", "c"}:
                return "Towered"
            if ("tower" in s and "non" not in s) or ("twr" in s):
                return "Towered"
            return "Non-Towered"

        if "towered_status" not in df_airports.columns:
            df_airports["towered_status"] = "Non-Towered"
        df_airports["towered_status"] = df_airports["towered_status"].map(_canon_towered_status)

        # Merge visits -> airport metadata/coords (same columns as your map)
        merge_cols = ["norm_id", "airport_id", "name", "state", "lat", "long", "towered_status"]
        for c in merge_cols:
            if c not in df_airports.columns:
                # fail-closed: if missing columns, return empty rather than crash the map
                return jsonify({"items": [], "next_cursor": None, "meta": {"error": f"airports missing {c}"}})

        df_vis_plot = pd.merge(
            df_visits,
            df_airports[merge_cols],
            on="norm_id",
            how="left",
            suffixes=("", "_apt"),
        )
        df_vis_plot = df_vis_plot.dropna(subset=["lat", "long"]).copy()

        if df_vis_plot.empty:
            return jsonify({"items": [], "next_cursor": None, "meta": {"count": 0}})

        # Repair towered_status if merge didn't carry it
        if "towered_status" not in df_vis_plot.columns:
            df_vis_plot["towered_status"] = None
        df_vis_plot["towered_status"] = (
            df_vis_plot["towered_status"]
            .fillna("Non-Towered")
            .map(_canon_towered_status)
        )

        # Filter to bbox (load what they see)
        df_vis_plot["lat"] = pd.to_numeric(df_vis_plot["lat"], errors="coerce")
        df_vis_plot["long"] = pd.to_numeric(df_vis_plot["long"], errors="coerce")
        df_vis_plot = df_vis_plot.dropna(subset=["lat", "long"]).copy()

        df_vis_plot = df_vis_plot[
            (df_vis_plot["lat"] >= south) & (df_vis_plot["lat"] <= north) &
            (df_vis_plot["long"] >= west) & (df_vis_plot["long"] <= east)
        ].copy()

        if df_vis_plot.empty:
            return jsonify({"items": [], "next_cursor": None, "meta": {"count": 0}})

        # First visit per airport (same semantics as your map)
        if mode == "first":
            if "date_visited" in df_vis_plot.columns:
                df_vis_plot = df_vis_plot.sort_values("date_visited")
            df_vis_plot = df_vis_plot.drop_duplicates("norm_id", keep="first")

        # Paging
        df_vis_plot = df_vis_plot.reset_index(drop=False).rename(columns={"index": "_rowid"})
        total = int(len(df_vis_plot))
        start = cursor
        end = min(cursor + limit, total)
        page = df_vis_plot.iloc[start:end].copy()


        # Popup builder (duplicated for surgical isolation)
        
        items = []
        is_first = (mode == "first")

        for _, vr in page.iterrows():
            status = str(vr.get("towered_status") or "Non-Towered")
            towered = True if status == "Towered" else False

            disp_id = str(vr.get("airport_id", "") or "")
   

            # stable-ish id for client-side de-dupe
            if is_first:
                vid = str(vr.get("norm_id") or disp_id or vr.get("_rowid"))
            else:
                vid = str(vr.get("_rowid"))

            items.append({
                "id": vid,
                "lat": float(vr["lat"]),
                "lon": float(vr["long"]),
                "towered": towered,
                "airport_id": disp_id,
                "name": str(vr.get("name") or ""),
                "state": str(vr.get("state") or ""),
                "visit_date": str(vr.get("date_visited") or ""),
                "callsign": str(vr.get("callsign") or ""),
                "notes": str(vr.get("notes") or ""),
            })

        next_cursor = end if end < total else None
        return jsonify({
            "items": items,
            "next_cursor": next_cursor,
            "meta": {
                "mode": mode,
                "count": len(items),
                "total_in_bbox": total,
                "bbox": {"w": west, "s": south, "e": east, "n": north},
            }
        })
    except Exception as e:
        # fail-closed: never crash the app
        try:
            return jsonify({"items": [], "next_cursor": None, "meta": {"error": str(e)[:200]}}), 200
        except Exception:
            return ("", 200)
