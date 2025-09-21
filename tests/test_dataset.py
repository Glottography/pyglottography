import logging
import argparse

import pytest
from cldfbench import CLDFWriter
from shapely.geometry import shape, MultiPolygon, Point

from pyglottography.dataset import Dataset, valid_geometry


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


def test_valid_geometry():
    geo = {  # A self-intersecting polygon, with a line sticking out.
        'type': 'Polygon',
        'coordinates': [[
            [-1, 1],
            [1, 1],
            [0, 0],
            [-1, -1],
            [1, -1],
            [-2, 2],
        ]]
    }
    res = shape(valid_geometry(geo))
    assert isinstance(res, MultiPolygon)
    assert res.contains(Point(0, 0.5)) and res.contains(Point(0, -0.5))


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
    import shutil

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
