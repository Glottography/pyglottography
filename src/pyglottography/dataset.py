import json
import math
import shutil
import typing
import functools
import itertools
import subprocess
import collections
import dataclasses

from tqdm import tqdm
import spherely
from shapely import make_valid, difference, simplify, remove_repeated_points
from shapely.geometry import shape, Point, MultiPolygon, Polygon, GeometryCollection, mapping
from shapely.ops import unary_union
from simplepybtex.database import parse_file
from clldutils.path import ensure_cmd
from clldutils.jsonlib import update_ordered, load, dump
from clldutils.misc import slug
from clldutils.markup import add_markdown_text
import cldfbench
from csvw.dsv import UnicodeWriter, reader
from csvw.dsv_dialects import Dialect
from cldfgeojson import MEDIA_TYPE, aggregate, feature_collection, merged_geometry
from cldfgeojson.create import shapely_simplified_geometry, shapely_fixed_geometry
from pycldf.sources import Sources, Source
import numpy as np

from .util import Feature, bbox

OBSOLETE_PROPS = ['reference', 'map_image_file', 'url']


def read_csv(p):
    # We allow comments in CSV files.
    return reader(p, dicts=True, dialect=Dialect())


def get_one_source(p, bibkey=None) -> typing.Optional[typing.Tuple[Source, str]]:
    """
    Read the first entry from a BibTeX file.

    :param p: Path of a BibTeX file.
    :param bibkey: If supplied, `bibkey` will be used as identifier of the returned `Source` object.
    :return: A `Source` object representing the first entry in the file and the original bibkey.
    """
    for key, entry in parse_file(str(p), 'bibtex').entries.items():
        return Source.from_entry(bibkey or key, entry), key


def valid_geometry(geometry):
    shp = shape(geometry)
    if not shp.is_valid:  # We fix invalid geometries.
        res = make_valid(shp)
        if isinstance(res, GeometryCollection):
            # The way shapely fixes MultiPolygon geomtries sometimes results in a
            # GeometryCollection - the "main" geometry, and some things that may be
            # pruned, like LineStrings or tiny Polygons.
            res = [
                s for s in res.geoms
                if isinstance(s, (Polygon, MultiPolygon)) and s.area > 1e-15]
            assert len(res) == 1
            res = res[0]
        assert isinstance(res, (Polygon, MultiPolygon)) and res.is_valid
        geometry = res.__geo_interface__
    return geometry


def merge_features_by_glottocode(features, check_only=False):
    """Merge features sharing the same glottocode."""
    by_gc = collections.defaultdict(list)
    no_gc = []
    for f in features:
        gc = f.get('properties', {}).get('cldf:languageReference')
        if gc:
            by_gc[gc].append(f)
        else:
            no_gc.append(f)

    # Check for duplicates
    duplicates = {gc: len(group) for gc, group in by_gc.items() if len(group) > 1}

    if check_only:
        if duplicates:
            print(f"Found {len(duplicates)} glottocodes with multiple features: {duplicates}")
            return True
        else:
            print("No duplicate glottocodes")
            return False

    # Merge each group
    merged = []
    for gc, group in by_gc.items():
        if len(group) == 1:
            merged.append(group[0])
        else:
            geoms = [shape(f['geometry']) for f in group]
            merged_geom = unary_union(geoms)
            new_feature = dict(group[0])
            new_feature['geometry'] = mapping(merged_geom)

            # Merge properties that may differ across features
            new_feature['properties'] = dict(new_feature['properties'])
            new_feature['properties']['name'] = merge_property_values(group, 'name')
            new_feature['properties']['year'] = merge_property_values(group, 'year')

            # Check if any feature has number_legend - if so, merge map and legend as pairs
            has_legend = any(f['properties'].get('number_legend') for f in group)
            if has_legend:
                merged_maps, merged_legends = merge_map_and_legend(group)
                new_feature['properties']['map_name_full'] = merged_maps
                new_feature['properties']['number_legend'] = merged_legends

                # Reconstruct 'maps' from the merged map_name_full and number_legend
                map_list = [m.strip() for m in merged_maps.split('|') if m.strip()]
                legend_list = [lg.strip() for lg in merged_legends.split('|') if lg.strip()]
                while len(legend_list) < len(map_list):
                    legend_list.append('')
                new_feature['properties']['maps'] = [
                    '{} [{}]'.format(m, lg) for m, lg in zip(map_list, legend_list)]
            else:
                new_feature['properties']['map_name_full'] = merge_property_values(group, 'map_name_full')

            shapely_fixed_geometry(new_feature)
            merged.append(new_feature)

    # Re-enumerate IDs after merging
    result = merged + no_gc
    for i, f in enumerate(result, start=1):
        f['properties']['id'] = str(i)

    return result


def merge_property_values(group, prop_name):
    """Merge property values from multiple features, concatenating unique values with ' | '."""
    values = [f['properties'].get(prop_name, '') for f in group]

    # Check if all values are the same
    if len(set(values)) == 1:
        return values[0]

    # Split all values by "|", collect unique entries
    all_entries = []
    for v in values:
        entries = [e.strip() for e in str(v).split('|')]
        all_entries.extend(entries)

    # Remove duplicates while preserving order
    seen = set()
    unique = []
    for entry in all_entries:
        if entry and entry not in seen:
            seen.add(entry)
            unique.append(entry)

    return ' | '.join(unique)


def merge_map_and_legend(group):
    """
    Merge map_name_full and number_legend as paired values.

    Each entry in map_name_full corresponds positionally to an entry in number_legend.
    When merging, we keep these pairs together and deduplicate based on the pair.
    """
    # Collect all (map, legend) pairs from all features
    all_pairs = []
    for f in group:
        maps = [m.strip() for m in str(f['properties'].get('map_name_full', '')).split('|')]
        legends = [lg.strip() for lg in str(f['properties'].get('number_legend', '')).split('|')]

        # Pad legends if shorter than maps
        while len(legends) < len(maps):
            legends.append('')

        for m, lg in zip(maps, legends):
            if m:  # Only add if map name is not empty
                all_pairs.append((m, lg))

    # Remove duplicate pairs while preserving order
    seen = set()
    unique_pairs = []
    for pair in all_pairs:
        if pair not in seen:
            seen.add(pair)
            unique_pairs.append(pair)

    if not unique_pairs:
        return '', ''

    merged_maps = ' | '.join(p[0] for p in unique_pairs)
    merged_legends = ' | '.join(p[1] for p in unique_pairs)

    return merged_maps, merged_legends


def azimuth(p1, p2):
    """Calculate azimuth from p1 to p2 (degrees from north, clockwise)."""
    dx = p2[0] - p1[0]
    dy = p2[1] - p1[1]
    return np.degrees(np.arctan2(dx, dy)) % 360


def angle_at_vertex(p1, p2, p3):
    """
    Calculate interior angle at vertex p2 using azimuth-based approach (like QGIS).
    """
    azimuth1 = azimuth(p1, p2)  # Direction of incoming edge
    azimuth2 = azimuth(p2, p3)  # Direction of outgoing edge

    if azimuth1 > azimuth2 and azimuth1 > azimuth2 + 180:
        angle = 540 - azimuth1 + azimuth2
    elif azimuth1 > azimuth2:
        angle = 180 - azimuth1 + azimuth2
    elif azimuth1 < azimuth2 and azimuth1 + 180 > azimuth2:
        angle = 180 + azimuth2 - azimuth1
    else:  # azimuth1 < azimuth2 and azimuth1 + 180 <= azimuth2
        angle = azimuth2 - azimuth1 - 180

    return angle


def remove_spikes_by_angle(coords, min_angle=1.0):
    """Remove vertices with angles below min_angle threshold."""
    coords = list(coords)
    if len(coords) < 4:
        return coords

    n = len(coords) - 1
    cleaned = [
        coords[i] for i in range(n)
        if angle_at_vertex(coords[(i - 1) % n], coords[i], coords[(i + 1) % n]) >= min_angle
    ]

    # Make sure start/end vertice is
    if cleaned and cleaned[0] != cleaned[-1]:
        cleaned.append(cleaned[0])

    return cleaned


def remove_spikes_from_polygon(geom, min_angle=1.0):
    """Remove spike vertices from polygon geometry."""
    if isinstance(geom, Polygon):
        exterior = remove_spikes_by_angle(geom.exterior.coords, min_angle)
        interiors = [
            remove_spikes_by_angle(ring.coords, min_angle)
            for ring in geom.interiors
        ]
        interiors = [i for i in interiors if len(i) >= 4]

        if len(exterior) >= 4:
            return Polygon(exterior, interiors)
        return None

    elif isinstance(geom, MultiPolygon):
        polys = [remove_spikes_from_polygon(p, min_angle) for p in geom.geoms]
        polys = [p for p in polys if p is not None]
        return MultiPolygon(polys) if polys else None

    return geom


def fix_spherical_geometries(features):
    """
    Check for spherically invalid geometries and attempt to auto-fix them.

    Uses spherely to detect invalidity in spherical geometry (S2),
    then applies four auto-fix steps:
    1. Remove repeated points
    2. Unary union
    3. Minimal buffer
    4. Minimal simplification

    :param features: List of GeoJSON feature dicts
    :return: List of features with fixed geometries
    """
    fixed_features = []
    fixed_count = 0

    for f in features:
        geom = shape(f['geometry'])

        # Check spherical validity
        try:
            spherely.from_wkt(geom.wkt)
            # Valid - keep as is
            fixed_features.append(f)
        except RuntimeError:
            # Invalid - attempt auto-fix
            fixed_geom = remove_repeated_points(geom, 0.00001)
            fixed_geom = unary_union(fixed_geom)
            fixed_geom = fixed_geom.buffer(0.0000001)
            fixed_geom = simplify(fixed_geom, 0.000009)
            fixed_geom = fixed_geom.buffer(-0.00001).buffer(0.00001)

            # Check if fix worked
            try:
                spherely.from_wkt(fixed_geom.wkt)
                # Fixed successfully
                new_feature = dict(f)
                new_feature['geometry'] = mapping(fixed_geom)
                fixed_features.append(new_feature)
                fixed_count += 1
            except RuntimeError:
                # Still invalid - try spike removal on original planar geometry
                fname = f.get('properties', {}).get('name', '?')
                # print(f"\nProcessing feature: {fname}")
                # print(f"  Exterior coords: {len(geom.exterior.coords) if hasattr(geom, 'exterior') else 'MultiPolygon'}")
                spike_fixed_geom = remove_spikes_from_polygon(geom)
                if spike_fixed_geom is not None:
                    try:
                        spherely.from_wkt(spike_fixed_geom.wkt)
                        # Spike removal fixed it
                        new_feature = dict(f)
                        new_feature['geometry'] = mapping(spike_fixed_geom)
                        fixed_features.append(new_feature)
                        fixed_count += 1
                        continue
                    except RuntimeError:
                        pass

                # Still invalid - keep original and warn
                print(f"Warning: Could not fix spherical geometry for feature "
                      f"{f.get('properties', {}).get('id', '?')}")
                fixed_features.append(f)

    if fixed_count > 0:
        print(f"Auto-fixed {fixed_count} spherically invalid geometries")

    return fixed_features


@dataclasses.dataclass
class FeatureSpec:
    """
    Provides metadata for a feature, in addition to the information provided in the source.

    ..seealso:: https://datatracker.ietf.org/doc/html/rfc7946#section-3.2
    """
    id: str
    name: str
    year: str
    glottocode: typing.Optional[str]
    properties: collections.OrderedDict

    @classmethod
    def from_row(cls, row):
        return cls(
            id=row.pop('id'),
            name=row.pop('name'),
            year=row.pop('year'),
            glottocode=row.pop('glottocode') or None,
            properties=row,
        )

    def as_row(self):
        res = collections.OrderedDict()
        for field in dataclasses.fields(self):
            if field.name == 'properties':
                res.update(getattr(self, field.name))
            else:
                res[field.name] = getattr(self, field.name) or ''
        return res


def recompute_shape(row, featuredict, feature_specs):
    """
    Recompute the geometry for a feature based on the specification in `row`.

    :param row:
    :param featuredict:
    :param feature_specs:
    :return:
    """
    tfid = row['source_fid']
    f = featuredict.get(tfid, dict(properties=feature_specs[tfid].as_row(), type='Feature'))
    if row['subtract']:
        f['geometry'] = featuredict[row['source_fid']]['geometry']
        for sfid in row['subtract'].split():
            f['geometry'] = difference(
                shape(f['geometry']), shape(featuredict[sfid]['geometry'])).__geo_interface__
        if f['geometry']['type'] == 'MultiPolygon':  # pragma: no cover
            # Remove artefacts created by not exactly matched shapes.
            f['geometry']['coordinates'] = [
                p for p in f['geometry']['coordinates']
                if shape(dict(type='Polygon', coordinates=p)).area > 0.001]
            if row.get('num_polys'):
                f['geometry']['coordinates'] = sorted(
                    f['geometry']['coordinates'],
                    key=lambda p: shape(dict(type='Polygon', coordinates=p)).area,
                    reverse=True)[:int(row['num_polys'])]
    elif row['replace']:
        f['geometry'] = featuredict[row['replace']]['geometry']
    if not f['geometry']['coordinates']:
        raise ValueError()  # pragma: no cover
    assert Feature(f).shape, f['properties']


class Move:
    """
    Implements the moving (or copying) of a single Polygon from one feature to another.
    """
    def __init__(self, row):
        self.source = row['source_fid']
        self.target = row['target_fid'].split()
        self.point = Point(float(row['longitude']), float(row['latitude']))
        self.poly = None

    def extracted(self, feature):
        assert feature['properties']['id'] == self.source, 'expected {} gor {}'.format(
            self.source, feature['properties']['id'])
        if feature['geometry']['type'] == 'Polygon':
            feature['geometry']['type'] = 'MultiPolygon'
            feature['geometry']['coordinates'] = [feature['geometry']['coordinates']]
        assert feature['geometry']['type'] == 'MultiPolygon', feature['geometry']['type']
        for i, poly in enumerate(feature['geometry']['coordinates']):
            if shape(dict(type='Polygon', coordinates=poly)).contains(self.point):
                self.poly = poly
                break
        else:
            return False  # pragma: no cover
        del feature['geometry']['coordinates'][i]
        return True

    def append(self, feature):
        assert self.poly
        if feature['geometry']['type'] == 'Polygon':
            feature['geometry']['type'] = 'MultiPolygon'
            feature['geometry']['coordinates'] = [feature['geometry']['coordinates']]
        feature['geometry']['coordinates'].append(self.poly)
        shp = shape(feature['geometry'])
        if not shp.is_valid:  # pragma: no cover
            shapely_fixed_geometry(feature)


class Dataset(cldfbench.Dataset):
    """
    An augmented `cldfbench.Dataset`
    """
    # Derived datasets can override the default directory in glottography-data from which to
    # download the raw data.
    _sdir = None
    _buffer = 0.005

    @functools.cached_property
    def feature_inventory_path(self):
        return self.etc_dir / 'features.csv'

    @property
    def feature_inventory(self):
        if self.feature_inventory_path.exists():
            res = collections.OrderedDict()
            for row in reader(self.feature_inventory_path, dicts=True):
                spec = FeatureSpec.from_row(row)
                res[spec.id] = spec
            return res

    @feature_inventory.setter
    def feature_inventory(self, value):
        with UnicodeWriter(self.feature_inventory_path) as writer:
            writer.writerows([row.as_row() for row in value])

    def iter_features(self):
        """
        Three error correction mechanisms are implemented:
        - recomputing the geometries of features (from geometries of other features),
        - moving polygons between features.
        - replacing the geometry of a feature with a new one, as specified in a GeoJSON file.

        :return:
        """
        fi = self.feature_inventory
        # Check if we have to move polygons around:
        moves, fixpolys = None, collections.defaultdict(list)
        if self.etc_dir.joinpath('move_polygons.csv').exists():
            # FIXME: allow for multiple moves per feature!
            moves = [Move(r) for r in read_csv(self.etc_dir / 'move_polygons.csv')]
            moves = {
                fid: list(ms) for fid, ms in itertools.groupby(
                    sorted(moves, key=lambda m: m.source), lambda m: m.source)}

        features = []
        for f in self.raw_dir.read_json('dataset.geojson')['features']:
            repl = self.etc_dir / '{}.geojson'.format(f['properties']['id'])
            if repl.exists():
                features.extend(load(repl)['features'])
            else:
                features.append(f)

        remove = []
        if self.etc_dir.joinpath('recompute_polygons.csv').exists():
            features_by_id = {f['properties']['id']: f for f in features}
            for row in read_csv(self.etc_dir / 'recompute_polygons.csv'):
                try:
                    recompute_shape(row, features_by_id, fi)
                except ValueError:  # pragma: no cover
                    remove.append(row['source_fid'])

        features = [f for f in features if f['properties']['id'] not in remove]

        if moves:  # First pass, extracting polygons to move.
            for feature in features:
                todo = moves.pop(feature['properties']['id'], None)
                if todo:
                    for move in todo:
                        assert move.extracted(feature), (feature['properties'], move.point)
                        for tfid in move.target:
                            fixpolys[tfid].append(move)

        for f in tqdm(features):
            fid = str(f['properties']['id'])
            if fid in fixpolys:
                for poly in fixpolys[fid]:
                    poly.append(f)
                del fixpolys[fid]
            spec = fi[fid]
            assert f['properties']['name'] == spec.name or spec.properties.get('note'), (
                '{} vs. {}'.format(spec.name, f['properties']['name']))
            f['properties']['year'] = spec.year
            if spec.glottocode:
                f['properties']['cldf:languageReference'] = spec.glottocode
            f['properties'].update(spec.properties)
            yield (fid, Feature(f), spec.glottocode)

        for fid, items in fixpolys.items():
            spec = fi[fid]
            yield (fid,
                   Feature(dict(
                       type='Feature',
                       properties=spec.as_row(),
                       geometry=dict(type='MultiPolygon', coordinates=[m.poly for m in items]))),
                   spec.glottocode)

        assert not moves, 'Not all specified moves executed'

    @functools.cached_property
    def features(self):
        return list(self.iter_features())

    @functools.cached_property
    def bounds(self):
        res = bbox([f for _, f, _ in self.features])
        return (
            math.floor(res[0] * 10) / 10,
            math.floor(res[1] * 10) / 10,
            math.ceil(res[2] * 10) / 10,
            math.ceil(res[3] * 10) / 10,
        )

    def bounding_box_as_feature(self):
        minlon, minlat, maxlon, maxlat = self.bounds
        coords = [[
            (minlon, minlat), (minlon, maxlat), (maxlon, maxlat), (maxlon, minlat), (minlon, minlat)
        ]]
        return Feature.from_geometry(dict(type='Polygon', coordinates=coords))

    def cmd_download(self, args):
        """
        Implements the creation of `raw/dataset.geojson` and `etc/features.csv` for the default case
        where a corresponding directory of the glottography-data repository exists.
        """
        # turn geopackage into geojson
        # turn polygon list into etc/polygons.csv
        # make sure list is complete and polygons are valid.
        sdir = self.dir.parent / 'glottography-data' / (self._sdir or self.id)
        if not sdir.exists():
            for d in self.dir.parent.joinpath('glottography-data').iterdir():
                if d.is_dir() and slug(d.name) == self.id:
                    sdir = d
                    break
            else:
                args.log.error('No matching data directory found')
                return

        sourcebib = self.etc_dir / 'sources.bib'
        if not sourcebib.exists():
            src, key = get_one_source(sdir / 'source' / '{}.bib'.format(sdir.name), bibkey=self.id)
            if key != sdir.name:
                args.log.warning('BibTeX key does not match dataset ID: {}'.format(key))
            sourcebib.write_text(src.bibtex(), encoding='utf-8')
        else:
            src, _ = get_one_source(sourcebib, bibkey=self.id)

        with update_ordered(self.dir / 'metadata.json', indent=4) as md:
            if not md.get('license'):
                md['license'] = 'CC-BY-4.0'
            if not md.get('title'):
                md['title'] = 'Glottography dataset derived from {} "{}"'.format(
                    src.refkey(year_brackets=None), src['title'])
            if not md.get('citation'):
                md['citation'] = str(src)

        # We want a valid geopackage:
        subprocess.check_call([
            ensure_cmd('ogr2ogr'),
            str(self.raw_dir / 'dataset.geojson'),
            str(sdir / '{}_raw.gpkg'.format(sdir.name)),
            '-t_srs', 'EPSG:4326',
            '-s_srs', 'EPSG:3857',
        ])
        features = {}
        with update_ordered(self.raw_dir / 'dataset.geojson') as geojson:
            # Rename polygon_id to id, delete unnecessary fields.
            for f in geojson['features']:
                f['properties']['id'] = str(f['properties'].pop('polygon_id'))
                for prop in OBSOLETE_PROPS:
                    f['properties'].pop(prop, None)
                features[f['properties']['id']] = f
                f['geometry'] = valid_geometry(f['geometry'])

        geometries = [
            shape(f['geometry']) for f in load(self.raw_dir / 'dataset.geojson')['features']]
        assert all(isinstance(p, (Polygon, MultiPolygon)) for p in geometries)
        assert all(p.is_valid for p in geometries)

        if not self.feature_inventory:
            args.log.info('creating polygon inventory')
            res = []
            for row in reader(sdir / '{}_glottocode_to_polygons.csv'.format(sdir.name), dicts=True):
                row['id'] = row.pop('polygon_id')
                shp = shape(features[row['id']]['geometry'])
                for prop in OBSOLETE_PROPS:
                    row.pop(prop, None)
                rpoint = Point(float(row.pop('lon')), float(row.pop('lat')))
                try:
                    assert shp.contains(rpoint) or shp.convex_hull.contains(rpoint)
                except AssertionError:  # pragma: no cover
                    args.log.warning('{}: {}'.format(shp.convex_hull.distance(rpoint), row))
                res.append(FeatureSpec.from_row(row))

            self.feature_inventory = res

        # Make sure the geo-data matches the CSV feature inventory:
        assert set(features.keys()) == set(self.feature_inventory.keys())

    def cmd_makecldf(self, args):
        # Write three sets of shapes:
        # 1. The shapes as they are in the source, aggregated by shape label, including
        #    fine-grained Glottocode(s) as available.
        # 2. The shapes aggregated by language-level Glottocodes.
        # 3. The shapes aggregated by family-level Glottocodes.
        self.schema(args.writer.cldf)
        self.local_schema(args.writer.cldf)
        args.writer.cldf.add_sources(*Sources.from_file(self.etc_dir / "sources.bib"))
        features = []
        fi = self.feature_inventory
        maps, with_maps = {}, False
        if self.etc_dir.joinpath('maps.csv').exists():
            with_maps = True
            for r in self.etc_dir.read_csv('maps.csv', dicts=True):
                args.writer.objects['ContributionTable'].append(
                    self.make_contribution_map(args, maps, r))

        for pid, f, gc in self.features:
            if not with_maps:
                if fi[pid].properties['map_name_full'] not in maps:
                    args.writer.objects['ContributionTable'].append(self.make_contribution_map(
                        args,
                        maps,
                        {},
                        id='map_{}'.format(len(maps) + 1),
                        name=fi[pid].properties['map_name_full']))
            f = self.make_feature(args, f)
            features.append(shapely_fixed_geometry(f))
            args.writer.objects['ContributionTable'].append(
                self.make_contribution_feature(
                    args,
                    pid,
                    gc,
                    f,
                    fi[pid],
                    [maps[mname.strip()]['ID']
                     for mname in fi[pid].properties['map_name_full'].split('|')]
                ))



        # merge features by glottocode
        features = merge_features_by_glottocode(features, check_only=False)

        # fix spherically invalid geometries
        features = fix_spherical_geometries(features)

        dump(
            feature_collection(
                features,
                **{
                    'description': self.metadata.description,
                    'dc:isPartOf': self.metadata.title,
                }),
            self.cldf_dir / 'features.geojson')
        args.writer.objects['MediaTable'].append(dict(
            ID='features',
            Name='Areas depicted in the source',
            Media_Type=MEDIA_TYPE,
            Download_URL='features.geojson',
        ))

        lids = None
        for ptype in ['dialect', 'language', 'family']:
            label = 'languages' if ptype == 'language' else \
                ('dialects' if ptype == 'dialect' else 'families')
            p = self.cldf_dir / '{}.geojson'.format(label)
            features, languages = aggregate(
                [(pid, f, gc) for pid, f, gc in self.features if gc],
                args.glottolog.api,
                level=ptype,
                buffer=self._buffer,
                opacity=0.5)
            if ptype == 'family':
                # For the shapes aggregated on family level, we make sure the GoeJSON doesn't get
                # too big. If it would get close to 1MB, we simplify the geometry.
                for f in features:
                    if len(json.dumps(f)) > 1000000:  # pragma: no cover
                        shapely_fixed_geometry(shapely_simplified_geometry(f))
            dump(
                feature_collection(
                    features,
                    title='Speaker areas for {}'.format(label),
                    description='Speaker areas aggregated for Glottolog {}-level languoids, '
                    'color-coded by family.'.format(ptype)),
                p)
            for (glang, pids, family), f in zip(languages, features):
                if lids is None or (glang.id not in lids):  # Don't append isolates twice!
                    args.writer.objects['LanguageTable'].append(dict(
                        ID=glang.id,
                        Name=glang.name,
                        Glottocode=glang.id,
                        Latitude=glang.latitude,
                        Longitude=glang.longitude,
                        Feature_IDs=map(str, pids),
                        Speaker_Area=p.stem,
                        Glottolog_Languoid_Level=ptype,
                        Family=family,
                    ))
            args.writer.objects['MediaTable'].append(dict(
                ID=p.stem,
                Name='Speaker areas for {}'.format(label),
                Description='Speaker areas aggregated for Glottolog {}-level languoids, '
                            'color-coded by family.'.format(ptype),
                Media_Type=MEDIA_TYPE,
                Download_URL=p.name,
            ))
            lids = {gl.id for gl, _, _ in languages}

        args.writer.cldf.properties['dc:spatial'] = \
            ('westlimit={:.1f}; southlimit={:.1f}; eastlimit={:.1f}; northlimit={:.1f}'.format(
                *self.bounds))
        assert args.writer.cldf.sources

    def make_feature(self, args, f: Feature) -> Feature:
        """
        Derived datasets can override this method to customize the GeoJSON feature objects form
        the source data.
        """
        return f

    def make_contribution_map(self, args, maps, md, **kw):
        md.update(kw)
        res = dict(
            ID=md['id'],
            Name=md['name'],
            Glottocode=None,
            Source=[self.id],
            Media_IDs=md.get('Media_IDs', []),
            Type='map',
            Description=md.get('description'),
            Year=None,
        )
        maps[res['Name']] = res
        return res

    def iter_map_files(self, cldfdir, map_, geotiff, web, bounds):
        """
        Copy the files and return a triple of `dict`s suitable as rows in MediaTable.
        """
        d = cldfdir / 'maps' / map_['ID']
        if not d.exists():
            d.mkdir(parents=True)
        for p, name, mimetype, description in [
            (geotiff, 'map.tif', 'image/tiff',
             'Georeferenced GeoTIFF image of the map for crs EPSG:4326'),
            (web, 'web.jpg', 'image/jpeg',
             'JPG translation of the georeferenced image reprojected to webmercator'),
            (bounds, 'web.jpg.bounds.geojson', 'application/geo+json',
             'The bounds of the JPG image of the map'),
        ]:
            target = d / name
            shutil.copy(p, target)
            yield dict(
                ID='{}_{}'.format(map_['ID'], name.split('.')[-1]),
                Name=name,
                Description=description,
                Download_URL=str(target.relative_to(cldfdir)),
                Media_Type=mimetype,
            )

    def make_contribution_feature(self,
                                  args,
                                  pid: str,
                                  gc: typing.Optional[str],
                                  f: Feature,
                                  fmd: FeatureSpec,
                                  map_ids: typing.List[str]) -> dict:
        """
        Derived datasets can override this method to customize the item in ContributionTable to be
        written for each source feature.
        """
        return dict(
            ID=pid,
            Name=f.properties['name'],
            Glottocode=gc or None,
            Source=[self.id],
            Media_IDs=['features'],
            Type='feature',
            Map_IDs=map_ids,
            Year=fmd.year,
        )

    def local_schema(self, cldf):
        """
        Derived datasets can override this method to customize the CLDF schema for the dataset.
        """
        pass

    def schema(self, cldf):
        cldf.add_component('MediaTable')
        cldf.add_component(
            'LanguageTable',
            {
                'name': 'Feature_IDs',
                'separator': ' ',
                'dc:description':
                    'List of identifiers of features that were aggregated '
                    'to create the feature referenced by Speaker_Area.',
                'propertyUrl': 'http://cldf.clld.org/v1.0/terms.rdf#contributionReference'
            },
            {
                "dc:description": "https://glottolog.org/meta/glossary#Languoid",
                "datatype": {
                    "base": "string",
                    "format": "dialect|language|family"
                },
                "name": "Glottolog_Languoid_Level"
            },
            {
                "name": "Family",
                "dc:description":
                    "Name of the top-level family for the languoid in the Glottolog classification."
                    " A null value in this column marks 1) top-level families in case "
                    "Glottolog_Languoid_Level is 'family' and 2) isolates in case "
                    "Glottolog_Languoid_Level is 'language'.",
            },
            {
                'name': 'Speaker_Area',
                'propertyUrl': 'http://cldf.clld.org/v1.0/terms.rdf#speakerArea'
            })
        t = cldf.add_component(
            'ContributionTable',
            {
                "datatype": {
                    "base": "string",
                    "format": "[a-z0-9]{4}[1-9][0-9]{3}"
                },
                "propertyUrl": "http://cldf.clld.org/v1.0/terms.rdf#glottocode",
                "valueUrl": "http://glottolog.org/resource/languoid/id/{Glottocode}",
                "name": "Glottocode",
                'dc:description':
                    'References a Glottolog languoid most closely matching the linguistic entity '
                    'described by the feature.',
            },
            {
                "name": "Year",
                "dc:description": "The time period to which the feature relates, specified as year "
                                  "AD or with the keyword 'traditional', meaning either the time "
                                  "of contact with European maritime powers or period when an "
                                  "ancient language was spoken.",
                "datatype": {
                    "base": "string",
                    "format": "[0-9]{3,4}|traditional"
                },
                "propertyUrl": "http://purl.org/dc/terms/temporal",
            },
            {
                'name': 'Source',
                'separator': ';',
                'propertyUrl': 'http://cldf.clld.org/v1.0/terms.rdf#source'
            },
            {
                'name': 'Media_IDs',
                'propertyUrl': 'http://cldf.clld.org/v1.0/terms.rdf#mediaReference',
                "dc:description": "Contributions of type 'feature' are linked to GeoJSON files "
                                  "that store the geo data.  can be related to various kinds of "
                                  "media. Contributions of type 'map' are linked to the "
                                  "corresponding scans of maps and geo-data derived from these.",
                "separator": " ",
            },
            {
                "dc:description": "There are two types of contributions: Individual geo-features "
                                  "as depicted in the source and images of maps.",
                "datatype": {
                    "base": "string",
                    "format": "map|feature"
                },
                "name": "Type"
            },
            {
                'name': 'Map_IDs',
                "separator": " ",
                'propertyUrl': 'http://cldf.clld.org/v1.0/terms.rdf#contributionReference',
                "dc:description": "Contributions of type 'feature' link to the maps on which "
                                  "they appear.",
            }
        )
        t.common_props['dc:description'] = \
            ('We list the individual features from the source dataset as contributions in order to '
             'preserve the original metadata and a point of reference for the aggregated shapes.')

    def cmd_readme(self, args):
        if len(self.features) > 500:  # pragma: no cover
            f = json.dumps(self.bounding_box_as_feature())
        else:
            max_geojson_len = getattr(args, 'max_geojson_len', 10000)
            shp = shape(merged_geometry([f for _, f, _ in self.features]))
            f = json.dumps(Feature.from_geometry(shp))
            if len(f) < 10 * max_geojson_len:
                tolerance = 0
                while len(f) > max_geojson_len and tolerance < 0.8:
                    tolerance += 0.1
                    f = json.dumps(Feature.from_geometry(simplify(shp, tolerance)))
            if len(f) > max_geojson_len:
                # Fall back to just a rectangle built from the bounding box.
                f = json.dumps(self.bounding_box_as_feature())
        res = add_markdown_text(
            cldfbench.Dataset.cmd_readme(self, args),
            """

### Coverage

```geojson
{}
```
""".format(f),
            'Description')
        if self.dir.joinpath('NOTES.md').exists():
            lines = self.dir.joinpath('NOTES.md').read_text(encoding='utf8').split('\n')
            text = '\n'.join('##' + line if line.startswith('#') else line for line in lines)
            res = add_markdown_text(res, text, "Coverage")
        return res
