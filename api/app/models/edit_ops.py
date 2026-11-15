"""The four per-layer edit capabilities (V109, decision-layer-edit-capabilities.md §3.1) as one wire shape:
what anyone with edit access may do on the layer. Enforced in authz.edit_allowed(op); not admin-overridable
(F1). Shared by the settings door (layer_shares), /me/layers and the field door — one shape, one name."""
from pydantic import BaseModel


class EditOps(BaseModel):
    add: bool = True
    attributes: bool = True
    geometry: bool = True
    delete: bool = True

    @classmethod
    def from_row(cls, add, attributes, geometry, delete) -> "EditOps":
        return cls(add=bool(add), attributes=bool(attributes), geometry=bool(geometry), delete=bool(delete))
