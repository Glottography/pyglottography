import logging
import argparse

import pytest
from cldfbench import CLDFWriter

from pyglottography.dataset import (
    Dataset, iter_merged_features_by_name_and_glottocode, merge_property_values, Move, FeatureSpec)


@pytest.fixture
def dataset(tmprepos):
    class D(Dataset):
        id = 'author2022word'
        dir = tmprepos

        def cmd_download(self, args):
            Dataset.cmd_download(self, args)
            fspec = self.etc_dir / 'features.csv'
            fspec_content = fspec.read_text(encoding='utf-8')
            fspec_content += '\n25x,name,,,Figure 1,,,'
            fspec.write_text(fspec_content)

    return D()


def test_Dataset_download_error(fixtures_dir, caplog):
    class D(Dataset):
        id = 'stuff'
        dir = fixtures_dir / 'author2022-word'

    ds = D()
    assert ds.cmd_download(argparse.Namespace(log=logging.getLogger(__name__))) is None
    assert caplog.records[-1].levelname == 'ERROR'


def test_Dataset_download(mocker, glottolog, dataset):
    dataset.cmd_download(argparse.Namespace(log=logging.getLogger(__name__)))
    dataset.etc_dir.joinpath('features.csv').unlink()
    # cmd_download is supposed to be idempotent.
    dataset.cmd_download(argparse.Namespace(log=logging.getLogger(__name__)))
    with CLDFWriter(cldf_spec=dataset.cldf_specs(), dataset=dataset) as writer:
        dataset.cmd_makecldf(argparse.Namespace(
            glottolog=mocker.Mock(api=glottolog),
            writer=writer,
            log=logging.getLogger(__name__),
        ))
    readme = dataset.cmd_readme(argparse.Namespace(
        log=logging.getLogger(__name__), max_geojson_len=5))
    assert 'includeme' in readme
    res = dataset.cmd_readme(argparse.Namespace(log=logging.getLogger(__name__)))
    assert 'geojson' in res


def test_Dataset_makecldf(dataset, mocker, glottolog):
    dataset.cmd_download(argparse.Namespace(log=logging.getLogger(__name__)))
    dataset.etc_dir.joinpath('maps.csv').write_text('id,name\nfig,Figure 1')

    with CLDFWriter(cldf_spec=dataset.cldf_specs(), dataset=dataset) as writer:
        assert len(list(dataset.iter_map_files(
            dataset.cldf_dir, dict(ID='m'), *(3 * [dataset.raw_dir / 'dataset.geojson'])))) == 3
        dataset.cmd_makecldf(argparse.Namespace(
            glottolog=mocker.Mock(api=glottolog),
            writer=writer,
            log=logging.getLogger(__name__),
        ))


def make_feature(topleft, bottomright, **props):
    props.setdefault('id', '1')
    props.setdefault('year', '2010')
    return dict(
        type='Feature',
        properties=props,
        geometry=dict(type='Polygon', coordinates= [
                [
                    [topleft[1], topleft[0]],
                    [topleft[1], bottomright[0]],
                    [bottomright[1], bottomright[0]],
                    [bottomright[1], topleft[0]],
                    [topleft[1], topleft[0]],
                ]
            ])
    )


def test_iter_merged():
    # Needs a geometry and a couple different properties objects.
    m = list(iter_merged_features_by_name_and_glottocode([
        (1, make_feature((10, 20), (-10, 40), name='Name', year='x'), 'abcd1234'),
        (1, make_feature((-5, 20), (-10, 50), name='Name', year='x'), 'abcd1234'),
    ]))
    assert len(m) == 1

    m = list(iter_merged_features_by_name_and_glottocode([
        (1, make_feature((10, 20), (-10, 40), name='Name', year='x'), 'abcd1234'),
        (1, make_feature((-5, 20), (-10, 50), name='Näme', year='x'), 'abcd1234'),
    ]))
    assert len(m) == 2

    m = list(iter_merged_features_by_name_and_glottocode([
        (1,
         make_feature(
             (10, 20), (-10, 40),
             name='Name',
             year='x',
             map_name_full='MName',
             number_legend='2'
         ),
         'abcd1234'),
        (1,
         make_feature((-5, 20), (-10, 50), name='Name', year='x'),
         'abcd1234'),
    ]))
    #print(m[0][1])
    assert m[0][1]['properties']['maps']


@pytest.mark.parametrize(
    'values,expected',
    [
        (['a'], 'a'),
        (['a', 'b'], 'a | b'),
        (['b', 'a', 'b'], 'b | a'),
        (['b', 'a | b'], 'b | a'),
    ]
)
def test_merge_property_values(values, expected):
    fgroup = [dict(properties=dict(n=v)) for v in values]
    assert merge_property_values(fgroup, 'n') == expected


def test_Move_force_multipolgon():
    f = Move.force_multipolygon(dict(geometry=dict(type='Polygon', coordinates=1)))
    assert f['geometry']['coordinates'] == [1]
    assert f['geometry']['type'] == 'MultiPolygon'


def test_FeatureSpec():
    f1 = FeatureSpec.from_row(dict(id='1', name='n', year='y', glottocode='g', note='n'))
    f2 = FeatureSpec.from_row(dict(id='2', name='n', year='y', glottocode='g', note='n'))
    # We ignore the feature ID for equality comoparison:
    assert f1 == f2
    assert FeatureSpec.merged('x', [f1, f2]).properties['note'] == 'n'

    f2.name = 'n2'
    assert FeatureSpec.merged('x', [f1, f2], name='+').name == 'n+n2'
