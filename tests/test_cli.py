from cldfbench.__main__ import main


def test_showmap(tmp_path, fixtures_dir):
    tp = tmp_path / 'test.html'
    main(args=[
        'glottography.showmap', str(fixtures_dir / 'author2022word' / 'cldf'), 'fig',
        '-o', str(tp), '--test'])
    assert tp.exists()
