# Changes


## Unreleased

- Added two mechanisms to fix shape geometries based on config data.
- Don't use cldfgeojson.dump since cutting off decimal places from coordinates may introduce
  invalid geometries.


## [0.1.0] - 2024-11-22

pyglottography provides
- a cldfbench project template to bootstrap new Glottography datasets,
- a `cldfbench.Dataset` subclass, implementing the Glottography publication workflow.

