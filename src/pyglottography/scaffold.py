import pathlib

from cldfbench.scaffold import Template

import pyglottography


class GlottographyTemplate(Template):
    package = 'pyglottography'

    dirs = Template.dirs + [pathlib.Path(pyglottography.__file__).parent / 'glottography_template']
