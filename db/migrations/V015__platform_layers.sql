-- V015 — platform.layers (layer registry + per-layer editability)
--
-- Layer 7b, decision doc D3. The THIRD authorization key: "is this layer editable
-- at all?" — a property of the layer, independent of who is asking. Separate axis
-- from OpenFGA access (the sharing key) and the Keycloak role (the capability key).
--
-- layer_id matches the OpenFGA object id (`layer:<layer_id>`) and the frontend
-- LayerDef.id — i.e. 'signs', 'street_lights'. `editable` defaults false
-- (AGO-faithful: published read-only until an owner turns editing on). For 7b the
-- values are set by hand via SQL; the admin toggle UI is deferred.
--
-- Audit columns mirror platform.users' created_at convention; updated_at is added
-- because editability is runtime state that gets flipped (the collection-event
-- scenario). updated_at is maintained by the SQL that flips `editable`.

CREATE TABLE platform.layers (
    layer_id    text PRIMARY KEY,
    editable    boolean     NOT NULL DEFAULT false,
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now()
);

-- Seed the two real layers. BOTH editable=true for now (the matrix's editability
-- toggle test later flips street_lights to false via a hand SQL UPDATE — D3/Part D).
INSERT INTO platform.layers (layer_id, editable) VALUES
    ('signs',         true),
    ('street_lights', true);
