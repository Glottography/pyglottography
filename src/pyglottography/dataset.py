import copy
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
from shapely import difference, simplify
from shapely.geometry import shape, Point, MultiPolygon, Polygon, mapping
from shapely.ops import unary_union
from simplepybtex.database import parse_file
from clldutils.path import ensure_cmd
from clldutils.jsonlib import update_ordered, load, dump
from clldutils.misc import slug
from clldutils.markup import add_markdown_text
import cldfbench
from csvw.dsv import UnicodeWriter, reader
from csvw.dsv_dialects import Dialect
from cldfgeojson import MEDIA_TYPE, aggregate, feature_collection
from cldfgeojson.geometry import shapely_simplified_geometry, fixed_geometry, merged_geometry
from pycldf.sources import Sources, Source

from .util import ReadonlyFeature, bbox

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


def iter_merged_features_by_name_and_glottocode(features):
    """Merge features sharing the same glottocode."""
    by_gc = collections.defaultdict(list)
    for fid, f, gc in features:
        by_gc[f['properties']['name'], gc, f['properties']['year']].append(f)

    # Merge each group
    merged = []
    for (name, gc, year), group in by_gc.items():
        fids = [f['properties']['id'] for f in group]
        if len(group) == 1:
            merged.append((group[0], gc, fids))
        else:
            merged_geom = unary_union([shape(fixed_geometry(f)['geometry']) for f in group])
            new_feature = copy.copy(group[0])
            new_feature['geometry'] = mapping(merged_geom)

            # Merge properties that may differ across features
            new_feature['properties'] = dict(new_feature['properties'])
            new_feature['properties']['name'] = merge_property_values(group, 'name')
            new_feature['properties']['year'] = year

            # Check if any feature has number_legend - if so, merge map and legend as pairs
            has_legend = any(f['properties'].get('number_legend') for f in group)
            if has_legend:
                merged_maps, merged_legends, _ = merge_map_and_legend_and_note(group)
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
                new_feature['properties']['map_name_full'] = merge_property_values(
                    group, 'map_name_full')

            fixed_geometry(new_feature)
            merged.append((new_feature, gc, fids))

    # Re-enumerate IDs after merging
    for i, (f, gc, fids) in enumerate(merged, start=1):
        f['properties']['id'] = str(i)
        fixed_geometry(f)
        yield str(i), f, gc, fids


def merge_property_values(group, prop_name):
    """Merge property values from multiple features, concatenating unique values with ' | '."""
    # Remove duplicates while preserving order
    seen = set()
    unique = []
    for f in group:
        v = f['properties'].get(prop_name, '')
        for entry in [e.strip() for e in str(v).split('|')]:
            if entry and entry not in seen:
                seen.add(entry)
                unique.append(entry)

    return ' | '.join(unique)


def merge_map_and_legend_and_note(group):
    """
    Merge map_name_full and number_legend as paired values.

    Each entry in map_name_full corresponds positionally to an entry in number_legend.
    When merging, we keep these pairs together and deduplicate based on the pair.
    """
    def get_prop(o, prop):
        props = getattr(o, 'properties', None) or o['properties']
        return str(props.get(prop, ''))

    # Collect all (map, legend) pairs from all features
    all_pairs, notes = [], set()
    for f in group:
        maps = [m.strip() for m in get_prop(f, 'map_name_full').split('|')]
        legends = [lg.strip() for lg in get_prop(f, 'number_legend').split('|')]
        notes.add(get_prop(f, 'note'))

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
        return '', '', '; '.join(sorted(notes))

    merged_maps = ' | '.join(p[0] for p in unique_pairs)
    merged_legends = ' | '.join(p[1] for p in unique_pairs)
    return merged_maps, merged_legends, '; '.join(sorted(notes))


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
    # We want to keep track of how features relate to the "raw" data, i.e. polygons in
    # raw/dataset.geojson and metadata in etc/features.csv.
    raw_ids: typing.List[str] = dataclasses.field(default_factory=list)

    @classmethod
    def from_row(cls, row):
        res = cls(
            id=row['id'],
            name=row.pop('name'),
            year=row.pop('year'),
            glottocode=row.pop('glottocode') or None,
            properties=row,
            raw_ids=[row.pop('id')],
        )
        res.normalize()
        return res

    def normalize(self):
        maps = [s.strip() for s in self.map_name_full.split('|')]
        numbers = [s.strip() for s in self.number_legend.split('|')]
        if set(numbers) == {''}:
            numbers = []
        if maps and numbers:
            assert len(maps) == len(numbers), '{}; {}'.format(maps, numbers)
        self.properties['map_name_full'] = ' | '.join(maps)
        self.properties['number_legend'] = ' | '.join(numbers)
        self.properties['maps'] = ['{} [{}]'.format(m, n) for m, n in zip(maps, numbers)]

    @classmethod
    def merged(cls, fid, specs, **merge_attrs):
        atts = {}
        for att in ['year', 'name', 'glottocode']:
            if att in merge_attrs:
                atts[att] = merge_attrs[att].join(
                    sorted({getattr(spec, att) or '' for spec in specs}))
            else:
                assert 1 == len({getattr(spec, att) for spec in specs}), (
                    "{}".format([getattr(spec, att) for spec in specs]))
                atts[att] = getattr(specs[0], att)

        res = cls(
            id=fid,
            properties=collections.OrderedDict(),
            raw_ids=list(itertools.chain(*[spec.raw_ids for spec in specs])),
            **atts,
        )
        m, l, n = merge_map_and_legend_and_note(specs)
        res.properties['map_name_full'] = m
        res.properties['number_legend'] = l
        if n:
            res.properties['note'] = n
        res.normalize()
        return res

    def as_row(self, omit=None):
        omit = omit or []
        omit.append('raw_ids')
        res = collections.OrderedDict()
        for field in dataclasses.fields(self):
            if field.name in omit:
                continue
            if field.name == 'properties':
                res.update(getattr(self, field.name))
            else:
                res[field.name] = getattr(self, field.name) or ''
        return res

    @property
    def map_name_full(self):
        return self.properties.get('map_name_full') or ''

    @property
    def number_legend(self):
        return self.properties.get('number_legend') or ''

    def __eq__(self, other):
        return self.as_row(omit=['id']) == other.as_row(omit=['id'])


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
    assert ReadonlyFeature(f).shape, f['properties']


class Move:
    """
    Implements the moving (or copying) of a single Polygon from one feature to another.
    """
    def __init__(self, row):
        self.source = row['source_fid']
        self.target = row['target_fid'].split()
        self.point = Point(float(row['longitude']), float(row['latitude']))
        self.poly = None

    @staticmethod
    def force_multipolygon(feature):
        if feature['geometry']['type'] == 'Polygon':
            feature['geometry']['type'] = 'MultiPolygon'
            feature['geometry']['coordinates'] = [feature['geometry']['coordinates']]
        return feature

    def extracted(self, feature):
        assert feature['properties']['id'] == self.source, 'expected {} gor {}'.format(
            self.source, feature['properties']['id'])
        feature = self.force_multipolygon(feature)
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
        feature = self.force_multipolygon(feature)
        feature['geometry']['coordinates'].append(self.poly)
        shp = shape(feature['geometry'])
        if not shp.is_valid:  # pragma: no cover
            fixed_geometry(feature)


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
        Reads "raw" data, i.e. geojson from raw/dataset.geojson and metadata from etc/features.csv.

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
            yield (fid, ReadonlyFeature(f), spec.glottocode)

        for fid, items in fixpolys.items():
            spec = fi[fid]
            yield (fid,
                   ReadonlyFeature(dict(
                       type='Feature',
                       properties=spec.as_row(),
                       geometry=dict(type='MultiPolygon', coordinates=[m.poly for m in items]))),
                   spec.glottocode)

        assert not moves, 'Not all specified moves executed'

    @functools.cached_property
    def features(self):
        """
        Merges features based on same name and glottocode, re-assigns serial identifiers.

        :return:
        """
        fi = self.feature_inventory
        res = []
        for fid, f, gc, fids in iter_merged_features_by_name_and_glottocode(self.iter_features()):
            res.append((fid, f, gc, FeatureSpec.merged(fid, [fi[ff] for ff in fids])))
        return res

    @functools.cached_property
    def bounds(self):
        res = bbox([f for _, f, _, _ in self.features])
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
        return ReadonlyFeature.from_geometry(dict(type='Polygon', coordinates=coords))

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
                fixed_geometry(f)

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
        maps, with_maps = {}, False
        if self.etc_dir.joinpath('maps.csv').exists():
            with_maps = True
            for r in self.etc_dir.read_csv('maps.csv', dicts=True):
                args.writer.objects['ContributionTable'].append(
                    self.make_contribution_map(args, maps, r))

        #
        # FIXME: fi must be merged as well!
        #

        for pid, f, gc, spec in self.features:
            if not with_maps:
                for mname in spec.properties['map_name_full'].split('|'):
                    mname = mname.strip()
                    if mname not in maps:
                        args.writer.objects['ContributionTable'].append(self.make_contribution_map(
                            args,
                            maps,
                            {},
                            id='map_{}'.format(len(maps) + 1),
                            name=mname))
            f = self.make_feature(args, f)
            features.append(fixed_geometry(f))
            args.writer.objects['ContributionTable'].append(
                self.make_contribution_feature(
                    args,
                    pid,
                    gc,
                    f,
                    spec,
                    [maps[mname.strip()]['ID']
                     for mname in spec.properties['map_name_full'].split('|')
                     if mname.strip() in maps]
                ))

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
                [(pid, f, gc) for pid, f, gc, _ in self.features if gc],
                args.glottolog.api,
                level=ptype,
                buffer=self._buffer,
                opacity=0.5)
            if ptype == 'family':
                # For the shapes aggregated on family level, we make sure the GoeJSON doesn't get
                # too big. If it would get close to 1MB, we simplify the geometry.
                for f in features:
                    if len(json.dumps(f)) > 1000000:  # pragma: no cover
                        fixed_geometry(shapely_simplified_geometry(f))
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

    def make_feature(self, args, f: ReadonlyFeature) -> ReadonlyFeature:
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
                                  f: ReadonlyFeature,
                                  fmd: FeatureSpec,
                                  map_ids: typing.List[str]) -> dict:
        """
        Derived datasets can override this method to customize the item in ContributionTable to be
        written for each source feature.
        """
        return dict(
            ID=pid,
            Name=f['properties']['name'],
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
            shp = shape(merged_geometry([f for _, f, _, _ in self.features]))
            f = json.dumps(ReadonlyFeature.from_geometry(shp))
            if len(f) < 10 * max_geojson_len:
                tolerance = 0
                while len(f) > max_geojson_len and tolerance < 0.8:
                    tolerance += 0.1
                    f = json.dumps(ReadonlyFeature.from_geometry(simplify(shp, tolerance)))
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
