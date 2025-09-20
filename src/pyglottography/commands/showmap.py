"""
Display a georeferenced map.
"""
import json
import pathlib
import webbrowser

from pycldf.media import File
from pycldf.cli_util import add_dataset, get_dataset
from clldutils.misc import data_url
from mako.lookup import Template

from pyglottography.util import bbox


def register(parser):
    add_dataset(parser)
    parser.add_argument('map-id')


def run(args):
    """
    Assemble geo-referenced scan and associated polygons.
    Format as standalone HTML page using leaflet.
    """
    out = pathlib.Path('.')

    def render(fname, **vars):
        out.joinpath(fname).write_text(
            Template(filename=str(pathlib.Path(__file__).parent / 'map.html.mako')).render(**vars),
            encoding='utf8')

    map_id = getattr(args, 'map-id')
    cldf = get_dataset(args)

    features = []
    for obj in cldf.objects('ContributionTable'):
        if obj.data['Type'] == 'feature' and map_id in obj.cldf.contributionReference:
            features.append(obj)
    m = cldf.get_object('ContributionTable', map_id)
    fids = {f.id for f in features}
    gcodes = {f.cldf.glottocode for f in features}

    gfeatures = [
        f for f in File.from_dataset(
            cldf, cldf.get_object('MediaTable', 'features')
        ).read_json()['features'] if f['properties']['id'] in fids]
    languages = [lg for lg in cldf.objects('LanguageTable') if lg.id in gcodes]

    img, bounds = None, None
    for f in m.all_related('mediaReference'):
        if f.id.endswith('_jpg'):
            img = File.from_dataset(cldf, f)
        elif f.id.endswith('_geojson'):
            bounds = File.from_dataset(cldf, f)

    if not bounds:
        bounds = bbox(gfeatures)
    else:
        bounds = bounds.read_json()['bbox']
    render(
        'index.html',
        map=m,
        img=data_url(img.read(), 'image/jpeg') if img else None,
        geojson=json.dumps(dict(type='FeatureCollection', features=gfeatures)),
        languages=sorted(languages, key=lambda lg: lg.cldf.name),
        lat1=bounds[1],
        lon1=bounds[0],
        lat2=bounds[3],
        lon2=bounds[2],
        w=4,
    )

    webbrowser.open(str(out / 'index.html'))
