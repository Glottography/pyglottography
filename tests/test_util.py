from shapely.geometry import shape

from pyglottography.util import ReadonlyFeature


def test_ReadonlyFeature():
    geometry = dict(type='Polygon', coordinates=[
        [[10, 20], [20, 20], [20, 30], [10, 30], [10, 20]]])
    f = ReadonlyFeature.from_geometry(shape(geometry), dict(id=5))
    assert f.properties['id'] == 5
