import typing
import functools
import itertools

from shapely.geometry import MultiPolygon, shape

__all__ = ['Feature', 'bbox']


class Feature(dict):
    """
    A (readonly) GeoJSON feature dict with syntactic sugar to access shape and properties.
    """
    @functools.cached_property
    def shape(self):
        return shape(self['geometry'])

    @functools.cached_property
    def properties(self):
        return self['properties']

    @classmethod
    def from_geometry(cls, geometry, properties=None):
        return cls(dict(
            type='Feature',
            geometry=getattr(geometry, '__geo_interface__', geometry),
            properties=properties or {}))


def bbox(features: typing.Iterable[Feature]) -> typing.List[float]:
    polys = list(itertools.chain(*[
        f.shape.geoms if isinstance(f.shape, MultiPolygon) else [f.shape]
        for f in (Feature(ff) if not isinstance(ff, Feature) else ff for ff in features)]))
    # minx, miny, maxx, maxy
    return list(MultiPolygon(polys).bounds)
