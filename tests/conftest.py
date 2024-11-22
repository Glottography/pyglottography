import shutil
import pathlib

import pytest


@pytest.fixture(scope='session')
def glottolog():
    from pyglottolog import Glottolog
    return Glottolog(pathlib.Path(__file__).parent / 'glottolog')


@pytest.fixture
def fixtures_dir():
    return pathlib.Path(__file__).parent / 'fixtures'


@pytest.fixture
def tmprepos(fixtures_dir, tmp_path):
    for d in fixtures_dir.iterdir():
        shutil.copytree(d, tmp_path / d.name)
    return tmp_path / 'author2022word'
