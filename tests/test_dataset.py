import logging
import argparse

from cldfbench import CLDFWriter

from pyglottography.dataset import Dataset


def test_Dataset_download_error(fixtures_dir, caplog):
    class D(Dataset):
        id = 'stuff'
        dir = fixtures_dir / 'author2022-word'

    ds = D()
    assert ds.cmd_download(argparse.Namespace(log=logging.getLogger(__name__))) is None
    assert caplog.records[-1].levelname == 'ERROR'


def test_Dataset_download(tmprepos, mocker, glottolog):
    class D(Dataset):
        id = 'author2022word'
        dir = tmprepos

    ds = D()
    ds.cmd_download(argparse.Namespace(log=logging.getLogger(__name__)))
    # cmd_download is supposed to be idempotent.
    ds.cmd_download(argparse.Namespace(log=logging.getLogger(__name__)))
    with CLDFWriter(cldf_spec=ds.cldf_specs(), dataset=ds) as writer:
        ds.cmd_makecldf(argparse.Namespace(
            glottolog=mocker.Mock(api=glottolog),
            writer=writer,
            log=logging.getLogger(__name__),
        ))
    ds.cmd_readme(argparse.Namespace(
        log=logging.getLogger(__name__), max_geojson_len=5))
    res = ds.cmd_readme(argparse.Namespace(log=logging.getLogger(__name__)))
    assert 'geojson' in res
