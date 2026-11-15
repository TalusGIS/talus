-- V118 — a caller's box or filter shape is converted into a layer's own coordinate system safely.
--
-- A layer stored in its own projected system ("native" storage) is filtered and tiled by converting the
-- caller's box INTO that system and comparing there, so the spatial index is used. A projected system
-- only covers part of the world. Asked to convert a point far outside it, the coordinate library either
-- raises an error (current versions: "Point outside of projection domain") or returns a meaningless
-- number (older versions). Both are wrong answers to "which rows are in this box": the first made a
-- world-scale tile or a far-away box answer with an error, the second made a natively stored layer
-- silently disappear from the widest zooms.
--
-- assets.shape_in_srid() answers the question properly. Every projected system in the catalog publishes
-- the area it is meant for (postgis_srs). The function widens that area by a margin and:
--   * the shape lies inside it      -> the plain conversion, exactly as before (the ordinary case);
--   * part of the shape lies inside -> that part is converted (with a vertex per degree, so the box's
--                                      edges follow the projection);
--   * none of it does               -> NULL, and a NULL box matches no row.
-- The absence of an error is NOT the test: the whole-world box converts without one into nonsense.
--
-- Consequence: a row stored more than 20 degrees outside its own system's published area is not found by
-- a box filter and is not drawn. Such a row is a coordinate in a system that does not cover it.
--
-- Not PARALLEL SAFE: it catches errors, and PostgreSQL cannot start that inside a parallel operation.
-- Callers put it in a scalar sub-select — (SELECT assets.shape_in_srid(...)) — so it is evaluated once
-- per query, not once per row.
--
-- Proven by scripts/verify-invariants (a far box, a world-scale tile and a far tile on a natively stored
-- layer). Tier-2, backup-gated.

CREATE OR REPLACE FUNCTION assets.shape_in_srid(shape geometry, target_srid integer)
RETURNS geometry
LANGUAGE plpgsql STABLE
AS $fn$
DECLARE
  v_auth text;
  v_code text;
  v_sw   geometry;
  v_ne   geometry;
  v_ll   geometry;   -- the shape in longitude/latitude
  v_area geometry;   -- where the target system is usable, widened
  v_clip geometry;
  v_pad  float8;
BEGIN
  IF shape IS NULL OR target_srid IS NULL THEN
    RETURN NULL;
  END IF;
  IF ST_SRID(shape) = target_srid THEN
    RETURN shape;
  END IF;

  SELECT auth_name, auth_srid::text INTO v_auth, v_code
  FROM public.spatial_ref_sys WHERE srid = target_srid;
  IF v_auth IS NOT NULL THEN
    SELECT point_sw, point_ne INTO v_sw, v_ne FROM public.postgis_srs(v_auth, v_code);
  END IF;

  -- No published area of use (a custom system), or one that crosses the antimeridian: nothing to
  -- clip against. Convert as is; a shape the system cannot hold matches nothing.
  IF v_sw IS NULL OR v_ne IS NULL OR ST_X(v_sw) > ST_X(v_ne) THEN
    BEGIN
      RETURN ST_Transform(shape, target_srid);
    EXCEPTION WHEN internal_error THEN
      RETURN NULL;
    END;
  END IF;

  BEGIN
    v_ll := CASE WHEN ST_SRID(shape) = 4326 THEN shape ELSE ST_Transform(shape, 4326) END;
  EXCEPTION WHEN internal_error THEN
    RETURN NULL;
  END;

  FOREACH v_pad IN ARRAY ARRAY[20.0, 2.0] LOOP
    v_area := ST_MakeEnvelope(
      GREATEST(ST_X(v_sw) - v_pad, -180), GREATEST(ST_Y(v_sw) - v_pad, -90),
      LEAST(ST_X(v_ne) + v_pad, 180),     LEAST(ST_Y(v_ne) + v_pad, 90), 4326);
    BEGIN
      IF ST_Covers(v_area, v_ll) THEN
        RETURN ST_Transform(shape, target_srid);          -- the ordinary case, unchanged
      END IF;
      v_clip := ST_Intersection(v_ll, v_area);
      IF v_clip IS NULL OR ST_IsEmpty(v_clip) THEN
        RETURN NULL;                                       -- no part of the shape is in reach
      END IF;
      RETURN ST_Transform(ST_Segmentize(v_clip, 1.0), target_srid);
    EXCEPTION WHEN internal_error THEN
      NULL;                                                -- try again with a narrower margin
    END;
  END LOOP;
  RETURN NULL;
END;
$fn$;

GRANT EXECUTE ON FUNCTION assets.shape_in_srid(geometry, integer) TO talus_app, talus_tiles;

COMMENT ON FUNCTION assets.shape_in_srid(geometry, integer) IS
    'A caller''s box or filter shape, converted into the given coordinate system for comparison with stored '
    'geometry. NULL when no part of the shape can be expressed in that system (it then matches no row). '
    'Clips to the system''s published area of use, widened by a margin, before converting.';

-- The tile function: the same body as before, with the tile's box converted by the function above.
-- (No longer marked PARALLEL SAFE, for the reason given above.)
CREATE OR REPLACE FUNCTION assets.slot_mvt(z integer, x integer, y integer, layer text DEFAULT '')
RETURNS bytea
LANGUAGE plpgsql STABLE
AS $fn$
DECLARE
  v_srid integer;
  v_result bytea;
BEGIN
  IF layer !~ '^tal_[0-9a-f]{8}$' THEN
    RETURN NULL;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM platform.layers WHERE layer_id = layer AND geom_type <> 'none') THEN
    RETURN NULL;
  END IF;
  v_srid := Find_SRID('assets', layer, 'geom');
  EXECUTE format($q$
    SELECT ST_AsMVT(tile, %L, 4096, 'geom')
    FROM (
      SELECT ST_AsMVTGeom(ST_Transform(s.geom, 3857), ST_TileEnvelope($1, $2, $3)) AS geom,
             s.id::text AS id
      FROM assets.%I s
      WHERE s.geom && (SELECT assets.shape_in_srid(ST_TileEnvelope($1, $2, $3), %s))
    ) AS tile
    WHERE tile.geom IS NOT NULL
  $q$, layer, layer, v_srid)
  INTO v_result
  USING z, x, y;
  RETURN v_result;
END;
$fn$;
