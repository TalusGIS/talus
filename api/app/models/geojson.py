from typing import Generic, Literal, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T", bound=BaseModel)


class Point(BaseModel):
    type: Literal["Point"] = "Point"
    coordinates: list[float] = Field(..., min_length=2, max_length=3)


class Feature(BaseModel, Generic[T]):
    type: Literal["Feature"] = "Feature"
    id: str | None = None
    geometry: Point
    properties: T


class FeatureCollection(BaseModel, Generic[T]):
    type: Literal["FeatureCollection"] = "FeatureCollection"
    features: list[Feature[T]]
