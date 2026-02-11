"""
Create release instructions and metadata.
"""
from cldfbench.cli_util import add_dataset_spec, get_dataset


def register(parser):
    add_dataset_spec(parser)
    parser.add_argument('tag')


def run(args):  # pragma: no cover
    tag = args.tag
    if not tag.startswith('v'):
        tag = 'v' + tag
    ds = get_dataset(args)
    ds.dir.joinpath('relnotes.txt').write_text(
        """\
Cite the source as

> {}

and the Glottography dataset as

DOI""".format(ds.metadata.citation),
        encoding='utf8')
    print('gh release create {} --title "{}" --notes-file relnotes.txt'.format(
        tag, ds.metadata.title.replace('"', r'\"')))
    print('')
    print("Now you should grab the Zenodo version DOI from\n"
          "https://zenodo.org/account/settings/github/repository/Glottography/{0}\n"
          "and add it to\n"
          "https://github.com/Glottography/{0}/releases/edit/{1}\n and the concept DOI to "
          "metadata.json".format(ds.dir.name, tag))
