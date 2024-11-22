import json
import math
import typing
import functools
import itertools
import subprocess
import collections
import dataclasses

from shapely import simplify
from shapely.geometry import shape, Point, MultiPolygon, Polygon
from pybtex.database import parse_file
from clldutils.path import ensure_cmd
from clldutils.jsonlib import dump, update_ordered
from clldutils.misc import slug
from clldutils.markup import add_markdown_text
import cldfbench
from csvw.dsv import UnicodeWriter, reader
from cldfgeojson import MEDIA_TYPE, aggregate, feature_collection, merged_geometry
from pycldf.sources import Sources, Source

OBSOLETE_PROPS = ['reference', 'map_image_file', 'url']


def get_one_source(p, bibkey=None):
    """
    Read the only entry from a BibTeX file.

    :param p:
    :return:
    """
    bib = parse_file(str(p), 'bibtex')
    assert len(bib.entries) == 1
    for key, entry in bib.entries.items():
        return Source.from_entry(bibkey or key, entry), key


class Feature(dict):
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


@dataclasses.dataclass
class FeatureSpec:
    """
    Provides metadata for a feature, in addition to the information provided in the source.

    ..seealso:: https://datatracker.ietf.org/doc/html/rfc7946#section-3.2
    """
    id: str
    name: str
    glottocode: typing.Optional[str]
    properties: collections.OrderedDict

    @classmethod
    def from_row(cls, row):
        return cls(
            id=row.pop('id'),
            name=row.pop('name'),
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


class Dataset(cldfbench.Dataset):
    """
    An augmented `cldfbench.Dataset`
    """
    @functools.cached_property
    def feature_inventory_path(self):
        return self.etc_dir / 'features.csv'

    @property
    def feature_inventory(self):
        if self.feature_inventory_path.exists():
            return [
                FeatureSpec.from_row(row)
                for row in reader(self.feature_inventory_path, dicts=True)]

    @feature_inventory.setter
    def feature_inventory(self, value):
        with UnicodeWriter(self.feature_inventory_path) as writer:
            for i, row in enumerate(value):
                assert isinstance(row, FeatureSpec)
                row = row.as_row()
                if i == 0:
                    writer.writerow(row.keys())
                writer.writerow(row.values())

    def iter_features(self):
        md = {p.id: p for p in self.feature_inventory}
        for f in self.raw_dir.read_json('dataset.geojson')['features']:
            fid = f['properties']['id']
            gc = md[fid].glottocode
            if gc:
                f['properties']['cldf:languageReference'] = gc
            yield (fid, Feature(f), gc)

    @functools.cached_property
    def features(self):
        return list(self.iter_features())

    @functools.cached_property
    def bounds(self):
        polys = list(itertools.chain(*[
            f.shape.geoms if isinstance(f.shape, MultiPolygon) else [f.shape]
            for _, f, _ in self.features]))
        # minx, miny, maxx, maxy
        res = MultiPolygon(polys).bounds
        return (
            math.floor(res[0] * 10) / 10,
            math.floor(res[1] * 10) / 10,
            math.ceil(res[2] * 10) / 10,
            math.ceil(res[3] * 10) / 10,
        )

    def cmd_download(self, args):
        # turn geopackage into geojson
        # turn polygon list into etc/polygons.csv
        # make sure list is complete and polygons are valid.
        sdir = self.dir.parent / 'glottography-data' / self.id
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

        geometries = [shape(f['geometry']) for f in features.values()]
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
                assert shp.contains(rpoint) or shp.convex_hull.contains(rpoint)
                res.append(FeatureSpec.from_row(row))

            self.feature_inventory = res

        # Make sure the geo-data matches the CSV feature inventory:
        assert set(features.keys()) == {f.id for f in self.feature_inventory}

    def cmd_makecldf(self, args):
        # Write three sets of shapes:
        # 1. The shapes as they are in the source, aggregated by shape label, including
        #    fine-grained Glottocode(s) as available.
        # 2. The shapes aggregated by language-level Glottocodes.
        # 3. The shapes aggregated by family-level Glottocodes.

        self.schema(args.writer.cldf)

        args.writer.cldf.add_sources(*Sources.from_file(self.etc_dir / "sources.bib"))
        features = []
        for pid, f, gc in self.features:
            features.append(f)
            args.writer.objects['ContributionTable'].append(dict(
                ID=pid,
                Name=f.properties['name'],
                Glottocode=gc or None,
                Source=[self.id],
                Media_ID='features',
                Map_Name=f.properties['map_name_full'],
            ))
        dump(dict(
            type='FeatureCollection',
            properties={
                'dc:description': self.metadata.description,
                'dc:isPartOf': self.metadata.title,
            },
            features=features), self.cldf_dir / 'features.geojson')
        args.writer.objects['MediaTable'].append(dict(
            ID='features',
            Name='Areas depicted in the source',
            Media_Type=MEDIA_TYPE,
            Download_URL='features.geojson',
        ))

        lids = None
        for ptype in ['language', 'family']:
            label = 'languages' if ptype == 'language' else 'families'
            p = self.cldf_dir / '{}.geojson'.format(label)
            features, languages = aggregate(
                [(pid, f, gc) for pid, f, gc in self.features if gc],
                args.glottolog.api,
                level=ptype,
                buffer=0.005,
                opacity=0.5)
            dump(
                feature_collection(
                    features,
                    title='Speaker areas for {}'.format(label),
                    description='Speaker areas aggregated for Glottolog {}-level languoids, '
                    'color-coded by family.'.format(ptype)),
                p,
                indent=2)
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
                'name': 'Source',
                'separator': ';',
                'propertyUrl': 'http://cldf.clld.org/v1.0/terms.rdf#source'
            },
            {
                'name': 'Media_ID',
                'propertyUrl': 'http://cldf.clld.org/v1.0/terms.rdf#mediaReference',
                'dc:description': 'Features are linked to GeoJSON files that store the geo data.'
            },
            {
                'name': 'Map_Name',
                'dc:description': 'Name of the map as given in the source publication.'
            }
        )
        t.common_props['dc:description'] = \
            ('We list the individual features from the source dataset as contributions in order to '
             'preserve the original metadata and a point of reference for the aggregated shapes.')

    def cmd_readme(self, args):
        max_geojson_len = getattr(args, 'max_geojson_len', 10000)
        shp = shape(merged_geometry([f for _, f, _ in self.features]))
        f = json.dumps(Feature.from_geometry(shp))
        tolerance = 0
        while len(f) > max_geojson_len and tolerance < 0.6:
            tolerance += 0.1
            f = json.dumps(Feature.from_geometry(simplify(shp, tolerance)))
        if len(f) > max_geojson_len:
            # Fall back to just a rectangle built from the bounding box.
            minlon, minlat, maxlon, maxlat = self.bounds
            coords = [[
                (minlon, minlat),
                (minlon, maxlat),
                (maxlon, maxlat),
                (maxlon, minlat),
                (minlon, minlat)
            ]]
            f = json.dumps(Feature.from_geometry(dict(type='Polygon', coordinates=coords)))
        return add_markdown_text(
            cldfbench.Dataset.cmd_readme(self, args),
            """

### Coverage

```geojson
{}
```
""".format(f),
            'Description')
