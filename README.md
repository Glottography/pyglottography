# pyglottography

Programmatic curation of Glottography datasets

[![Build Status](https://github.com/glottography/pyglottography/workflows/tests/badge.svg)](https://github.com/glottography/pyglottography/actions?query=workflow%3Atests)
[![PyPI](https://img.shields.io/pypi/v/pyglottography.svg)](https://pypi.org/project/pyglottography)


## Installation

Install via pip from [PyPI](https://pypi.org/project/pyglottography):
```shell
pip install pyglottography
```

> [!NOTE]
> We use GDAL's [ogr2ogr](https://gdal.org/en/latest/programs/ogr2ogr.html) command to convert between
> GeoJSON and GeoPackage formats. Thus, some functionality of `pyglottography` requires a working
> [GDAL](https://gdal.org/en/latest/index.html) installation.


## Curating Glottography datasets with `pyglottography`

### Bootstrapping a new dataset

`pyglottography` provides a [cldfbench project template](https://github.com/cldf/cldfbench?tab=readme-ov-file#custom-dataset-templates),
which can be used with the `cldfbench new` command:
```shell
cldfbench new --template glottography
```


### Providing the `raw` data

The `cldfbench` workflow uses data in a project's `raw` directory - enriched with information from
`etc` - to create a CLDF dataset in the `cldf` directory. By default, `pyglottography` expects input
data as follows:
- Geo-data, i.e. shapes for languoid areas, is expected in a GeoJSON file `raw/dataset.geojson`. Each
  feature in thie GeoJSON file should have a unique value for the `id` property.
- Metadata about the shapes is expected in a CSV file `etc/features.csv`. This file must have an `id`
  column with values corresponding to the feature `id`s in the geo-data.

While metadata could be read entirely from the `properties` object of features in the GeoJSON file,
`pyglottography` looks up the metadata in a different file to allow for more transparent curation.
Since the Glottolog language catalog is released in a new version about twice a year, it is necessary
to be able to recreate a Glottography dataset with updated Glottocodes. With the raw data setup as
implemented in `pyglottography`, this only requires changes in `etc/features.csv`, which can easily
be tracked with versioning software such as git.


### Running the CLDF creation

```shell
cldfbench makecldf cldfbench_<dsid>.py
```
